import json
import logging
import os
import re
from datetime import datetime, timedelta

import pandas as pd
import pytz
import requests
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from espn_scraper import run_scraper
from ncaaf_team_matching_helper import extend_mapping_with_schedule, resolve_team, save_mapping
from odds_service import enrich_predictions_with_odds, odds_api_enabled, select_best_bets

# ----------------------------------------------------------------------------- #
# Logging
# ----------------------------------------------------------------------------- #
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ----------------------------------------------------------------------------- #
# Static data / config
# ----------------------------------------------------------------------------- #
with open("team_logos.json", "r") as f:
    logo_data = json.load(f)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "your_secret_key")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg"

# TheSportsDB
API_KEY = "697039"
SPORT_LEAGUES = {
    "NBA": "4387",
    "MLB": "4424",
    "NCAAF": "4479",
    "NFL": "4391",
}

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BZBets/2.0; +https://example.com)",
    "Accept": "application/json",
}

# Sheets read cache
sheet_data_cache = {}  # {league_tab: (fetched_at_datetime, DataFrame)}
CACHE_TTL = timedelta(minutes=2)

LOCAL_TIMEZONE = pytz.timezone("America/Chicago")
SPORTSDB_TIMEZONE = pytz.utc

NCAAF_MAP_PATH = os.path.join(os.path.dirname(__file__), "ncaaf_team_mapping.json")
try:
    with open(NCAAF_MAP_PATH, "r", encoding="utf-8") as f:
        ncaaf_map = json.load(f)
except FileNotFoundError:
    ncaaf_map = {}

# Aliases (for logos & strict mapping for pro leagues only)
TEAM_ALIASES = {
    "Philadelphia 76ers": "76ers",
    "Milwaukee Bucks": "Bucks",
    "Chicago Bulls": "Bulls",
    "Cleveland Cavaliers": "Cavaliers",
    "Boston Celtics": "Celtics",
    "Los Angeles Clippers": "LA Clippers",
    "Memphis Grizzlies": "Grizzlies",
    "Atlanta Hawks": "Hawks",
    "Miami Heat": "Heat",
    "Charlotte Hornets": "Hornets",
    "Utah Jazz": "Jazz",
    "Sacramento Kings": "Kings",
    "New York Knicks": "Knicks",
    "Los Angeles Lakers": "Lakers",
    "LA Lakers": "Lakers",
    "Orlando Magic": "Magic",
    "Dallas Mavericks": "Mavericks",
    "Brooklyn Nets": "Nets",
    "Denver Nuggets": "Nuggets",
    "Indiana Pacers": "Pacers",
    "New Orleans Pelicans": "Pelicans",
    "Detroit Pistons": "Pistons",
    "Toronto Raptors": "Raptors",
    "Houston Rockets": "Rockets",
    "San Antonio Spurs": "Spurs",
    "Phoenix Suns": "Suns",
    "Oklahoma City Thunder": "Thunder",
    "Minnesota Timberwolves": "Timberwolves",
    "Portland Trail Blazers": "Trail Blazers",
    "Golden State Warriors": "Warriors",
    "Washington Wizards": "Wizards",
    "Los Angeles Angels": "Angels",
    "Houston Astros": "Astros",
    "Oakland Athletics": "Athletics",
    "Athletics": "Athletics",
    "Toronto Blue Jays": "Blue Jays",
    "Atlanta Braves": "Braves",
    "Milwaukee Brewers": "Brewers",
    "St. Louis Cardinals": "Cardinals",
    "Chicago Cubs": "Cubs",
    "Arizona Diamondbacks": "Diamondbacks",
    "Los Angeles Dodgers": "Dodgers",
    "San Francisco Giants": "Giants",
    "Cleveland Guardians": "Guardians",
    "Seattle Mariners": "Mariners",
    "Miami Marlins": "Marlins",
    "New York Mets": "Mets",
    "Washington Nationals": "Nationals",
    "Baltimore Orioles": "Orioles",
    "San Diego Padres": "Padres",
    "Philadelphia Phillies": "Phillies",
    "Pittsburgh Pirates": "Pirates",
    "Texas Rangers": "Rangers",
    "Tampa Bay Rays": "Rays",
    "Boston Red Sox": "Red Sox",
    "Cincinnati Reds": "Reds",
    "Colorado Rockies": "Rockies",
    "Kansas City Royals": "Royals",
    "Detroit Tigers": "Tigers",
    "Minnesota Twins": "Twins",
    "Chicago White Sox": "White Sox",
    "New York Yankees": "Yankees",
}

# ----------------------------------------------------------------------------- #
# NCAAF name helpers
# ----------------------------------------------------------------------------- #
NCAAF_NAME_ALIASES = {
    "USC": "Southern California",
    "LSU": "Louisiana State",
    "BYU": "Brigham Young",
    "UMass": "Massachusetts",
    "Ole Miss": "Mississippi",
    "Cal": "California",
    "Hawai'i": "Hawaii",
    "Hawai‘i": "Hawaii",
    "Arizona St": "Arizona State",
    "Ohio St": "Ohio State",
    "Penn St": "Penn State",
    "Florida St": "Florida State",
    "Kansas St": "Kansas State",
    "Utah St": "Utah State",
    "Colorado St": "Colorado State",
    "LA Tech": "Louisiana Tech",
    "SE Louisiana": "Southeastern Louisiana",
    "SE Missouri State": "Southeast Missouri State",
    "Long Island": "LIU",
    "LIU": "Long Island",
}


def _normalize_college_name(name: str) -> str:
    s = name.strip()
    if s in NCAAF_NAME_ALIASES:
        return NCAAF_NAME_ALIASES[s]
    s = s.replace("&", "and")
    s = s.replace("’", "'").replace("ʻ", "'").replace("`", "'")
    s = s.replace("'", "")
    s = re.sub(r"^(University of|Univ\. of|Univ of)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+University$", "", s, flags=re.I)
    s = re.sub(r"\bSt\.?\b", "State", s, flags=re.I)
    return s.strip()


def _ncaaf_variants(name: str):
    base = name.strip()
    norm = _normalize_college_name(base)
    variants = {base, norm}
    for k, v in NCAAF_NAME_ALIASES.items():
        if base == k:
            variants.add(v)
        if base == v:
            variants.add(k)
    if "State" in norm:
        variants.add(norm.replace("State", "St"))
    if "St " in norm:
        variants.add(norm.replace("St", "State"))
    tokens = norm.split()
    if len(tokens) >= 3:
        variants.add(" ".join(tokens[:2]))
    return [v for v in variants if v]


# ----------------------------------------------------------------------------- #
# Google Sheets
# ----------------------------------------------------------------------------- #
def fetch_data_from_sheets(league_tab: str) -> pd.DataFrame:
    now = datetime.utcnow()
    cached = sheet_data_cache.get(league_tab)
    if cached:
        fetched_at, df_cached = cached
        if now - fetched_at < CACHE_TTL:
            return df_cached.copy()

    sheet_id = os.environ.get("GOOGLE_SHEETS_ID", SPREADSHEET_ID)
    creds_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        os.path.join(os.path.dirname(__file__), "service_account.json"),
    )
    creds = Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    rng = f"{league_tab}!A1:D1000"
    res = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    values = res.get("values", [])

    if not values or len(values) < 2:
        df_empty = pd.DataFrame(
            columns=["G", "PF", "PA"], index=pd.Index([], name="Team")
        )
        sheet_data_cache[league_tab] = (now, df_empty.copy())
        return df_empty

    expected = ["Team", "G", "PF", "PA"]
    rows = []
    for r in values[1:]:
        if isinstance(r, str):
            r = [r]
        if not isinstance(r, list):
            r = [str(r)]
        r = (r + ["", "", "", ""])[:4]
        rows.append(r)

    df = pd.DataFrame(rows, columns=expected)
    df["Team"] = df["Team"].astype(str).str.strip()
    df = df[df["Team"] != ""]
    for c in ["G", "PF", "PA"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df.set_index("Team", inplace=True)

    sheet_data_cache[league_tab] = (now, df.copy())
    return df


# ----------------------------------------------------------------------------- #
# Schedules: SportsDB primary + ESPN fallback (NCAAF)
# ----------------------------------------------------------------------------- #
def _espn_ncaaf_from_site_scoreboard(ymd) -> list:
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/football/"
        f"college-football/scoreboard?dates={ymd}"
    )
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=20)
        if r.status_code != 200:
            logging.warning("ESPN site scoreboard %s: %s", r.status_code, url)
            return []
        data = r.json()
        events = data.get("events") or []
        out = []
        for ev in events:
            comps = ev.get("competitions") or []
            if not comps:
                continue
            comp = comps[0]
            iso_dt = comp.get("date")
            try:
                game_dt_utc = datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
                date_event = game_dt_utc.date().isoformat()
                str_time = game_dt_utc.strftime("%H:%M:%S")
            except Exception:
                continue
            teams = comp.get("competitors") or []
            home = next((t for t in teams if t.get("homeAway") == "home"), None)
            away = next((t for t in teams if t.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            home_name = (home.get("team") or {}).get("displayName")
            away_name = (away.get("team") or {}).get("displayName")
            if not home_name or not away_name:
                continue
            out.append(
                {
                    "strHomeTeam": home_name,
                    "strAwayTeam": away_name,
                    "dateEvent": date_event,
                    "strTime": str_time,
                }
            )
        return out
    except Exception as e:
        logging.warning("ESPN site scoreboard failed: %s", e)
        return []


def _espn_ncaaf_from_core_events(ymd) -> list:
    base = (
        "https://sports.core.api.espn.com/v2/sports/football/leagues/"
        f"college-football/events?dates={ymd}"
    )
    out = []
    try:
        r = requests.get(base, headers=HTTP_HEADERS, timeout=20)
        if r.status_code != 200:
            logging.warning("ESPN core events %s: %s", r.status_code, base)
            return out
        items = (r.json() or {}).get("items") or []
        for item in items:
            try:
                comps_ref = (item.get("competitions") or [{}])[0].get("$ref")
                if not comps_ref:
                    continue
                cr = requests.get(comps_ref, headers=HTTP_HEADERS, timeout=20)
                if cr.status_code != 200:
                    continue
                comp = cr.json()
                iso_dt = comp.get("date")
                game_dt_utc = datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
                date_event = game_dt_utc.date().isoformat()
                str_time = game_dt_utc.strftime("%H:%M:%S")

                competitors = comp.get("competitors") or []
                home = next(
                    (t for t in competitors if t.get("homeAway") == "home"), None
                )
                away = next(
                    (t for t in competitors if t.get("homeAway") == "away"), None
                )
                if not home or not away:
                    continue

                def _team_name(team_obj):
                    tref = (team_obj or {}).get("team", {}).get("$ref")
                    if tref:
                        tr = requests.get(tref, headers=HTTP_HEADERS, timeout=20)
                        if tr.status_code == 200:
                            return (tr.json() or {}).get("displayName")
                    return None

                home_name = _team_name(home)
                away_name = _team_name(away)
                if not home_name or not away_name:
                    continue

                out.append(
                    {
                        "strHomeTeam": home_name,
                        "strAwayTeam": away_name,
                        "dateEvent": date_event,
                        "strTime": str_time,
                    }
                )
            except Exception:
                continue
    except Exception as e:
        logging.warning("ESPN core events failed: %s", e)
    return out


def get_todays_games(league_name):
    league_id = SPORT_LEAGUES[league_name]
    today_local = datetime.now(LOCAL_TIMEZONE).date()

    def _is_game_today_local(game) -> bool:
        if not game.get("dateEvent") or not game.get("strTime"):
            return False

        dt_str = f"{game['dateEvent']} {game['strTime']}"
        try:
            naive = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            game_dt_local = pytz.utc.localize(naive).astimezone(LOCAL_TIMEZONE)
            return game_dt_local.date() == today_local
        except Exception as e:
            logging.warning(
                "Could not parse/convert schedule time '%s' for %s: %s",
                dt_str,
                league_name,
                e,
            )
            return False

    # NCAAF uses ESPN scoreboard/core events.
    if league_name == "NCAAF":
        ymd = today_local.strftime("%Y%m%d")
        espn_games = _espn_ncaaf_from_site_scoreboard(ymd) or _espn_ncaaf_from_core_events(
            ymd
        )
        games_today = [g for g in espn_games if _is_game_today_local(g)]
        logging.info(
            "NCAAF ESPN games for today (local): %s",
            [(g.get("strAwayTeam"), g.get("strHomeTeam")) for g in games_today],
        )
        return games_today

    # Pro leagues: SportsDB season feed.
    season_map = {
        "NBA": "2025-2026",
        "MLB": "2026",
        "NFL": "2026",
    }
    season = season_map.get(league_name, "2026")

    url = (
        f"https://www.thesportsdb.com/api/v1/json/{API_KEY}/"
        f"eventsseason.php?id={league_id}&s={season}"
    )
    sportsdb_games = []
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        raw_events = data.get("events") or []
        sportsdb_games = [g for g in raw_events if _is_game_today_local(g)]
    except Exception as e:
        logging.warning("SportsDB error for %s: %s", league_name, e)

    logging.info(
        "%s SportsDB games for today: %s",
        league_name,
        [(g.get("strAwayTeam"), g.get("strHomeTeam")) for g in sportsdb_games],
    )
    return sportsdb_games


# ----------------------------------------------------------------------------- #
# Matching helpers
# ----------------------------------------------------------------------------- #
def _pro_strict_match(team_name: str, team_list: list) -> str | None:
    if team_name in team_list:
        return team_name
    alias = TEAM_ALIASES.get(team_name)
    if alias and alias in team_list:
        return alias
    return None


def _ncaaf_strict_match(raw_name: str, team_list: list, stats_df: pd.DataFrame):
    try:
        primary, stats_key = resolve_team(raw_name, ncaaf_map, sheet_names=team_list)
        logging.info(
            "[NCAAF resolver] raw='%s' -> primary='%s', stats_key='%s'",
            raw_name,
            primary,
            stats_key,
        )
        for cand in (stats_key, primary):
            if cand and cand in team_list:
                try:
                    return cand, stats_df.loc[cand]
                except KeyError:
                    pass
    except Exception as e:
        logging.warning("resolve_team failed for '%s': %s", raw_name, e)

    for cand in _ncaaf_variants(raw_name):
        if cand in team_list:
            try:
                return cand, stats_df.loc[cand]
            except KeyError:
                pass

    logging.info("NCAAF strict drop - no exact Sheet match for '%s'", raw_name)
    return None, None


def get_team_logo(team_short_name, sport):
    full_name = next(
        (k for k, v in TEAM_ALIASES.items() if v == team_short_name),
        team_short_name,
    )
    try:
        return logo_data.get(sport, {}).get(full_name, None)
    except Exception as e:
        logging.warning("Logo lookup failed for %s: %s", team_short_name, e)
        return None


# ----------------------------------------------------------------------------- #
# Prediction pipeline
# ----------------------------------------------------------------------------- #
def predict_game_totals(league_name):
    predictions = []
    today_local = datetime.now(LOCAL_TIMEZONE).date()

    games = get_todays_games(league_name)
    logging.info("%s games fetched: %s", league_name, len(games))

    if league_name == "NCAAF":
        global ncaaf_map
        schedule_teams = {g.get("strHomeTeam") for g in games} | {
            g.get("strAwayTeam") for g in games
        }
        schedule_teams = {t for t in schedule_teams if t}
        try:
            ncaaf_map = extend_mapping_with_schedule(
                schedule_teams, ncaaf_map, sheet_names=None
            )
            save_mapping(NCAAF_MAP_PATH, ncaaf_map)
        except Exception as e:
            logging.warning("NCAAF mapping extend failed: %s", e)

    stats_df = fetch_data_from_sheets(league_name)
    team_list = stats_df.index.tolist()
    seen_matchups = set()

    def _per_game(row):
        try:
            g = float(row.get("G", 0)) or 0.0
            pf = float(row.get("PF", 0)) or 0.0
            pa = float(row.get("PA", 0)) or 0.0
            if g <= 0:
                return 0.0, 0.0
            return pf / g, pa / g
        except Exception:
            return 0.0, 0.0

    def _find_row(team_name):
        if league_name == "NCAAF":
            return _ncaaf_strict_match(team_name, team_list, stats_df)

        match = _pro_strict_match(team_name, team_list)
        if not match:
            logging.info(
                "%s strict drop - no exact Sheet match for '%s'",
                league_name,
                team_name,
            )
            return None, None
        try:
            return match, stats_df.loc[match]
        except KeyError:
            return None, None

    for game in games:
        home_raw = game.get("strHomeTeam")
        away_raw = game.get("strAwayTeam")
        if not home_raw or not away_raw:
            continue

        game_time_local = None
        if game.get("dateEvent") and game.get("strTime"):
            dt_str = f"{game['dateEvent']} {game['strTime']}"
            try:
                naive = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                utc_time = SPORTSDB_TIMEZONE.localize(naive)
                game_time_local = utc_time.astimezone(LOCAL_TIMEZONE)
            except Exception as e:
                logging.warning("Could not parse/convert time %s: %s", dt_str, e)

        if not game_time_local or game_time_local.date() != today_local:
            continue

        name_home, row_home = _find_row(home_raw)
        name_away, row_away = _find_row(away_raw)

        if not name_home or not name_away:
            logging.info(
                "Skipping matchup due to strict match failure: '%s' @ '%s'",
                away_raw,
                home_raw,
            )
            continue

        matchup_key = tuple(sorted([name_home, name_away]))
        if matchup_key in seen_matchups:
            continue
        seen_matchups.add(matchup_key)

        home_pfpg, home_papg = _per_game(row_home)
        away_pfpg, away_papg = _per_game(row_away)
        predicted_total = round(
            (home_pfpg + home_papg + away_pfpg + away_papg) / 2.0, 1
        )

        predictions.append(
            {
                "sport": league_name,
                "team1": name_away,
                "team2": name_home,
                "away_team_raw": away_raw,
                "home_team_raw": home_raw,
                "team1_logo": get_team_logo(name_away, league_name),
                "team2_logo": get_team_logo(name_home, league_name),
                "predicted_total": predicted_total,
                "game_time": game_time_local,
                "display_time": game_time_local.strftime("%I:%M %p"),
            }
        )

    return enrich_predictions_with_odds(predictions, league_name)


# ----------------------------------------------------------------------------- #
# Admin endpoints
# ----------------------------------------------------------------------------- #
def _check_admin_token():
    expected = os.environ.get("ADMIN_TOKEN", "")
    got = request.headers.get("X-Admin-Token", "")
    if not expected or got != expected:
        abort(401)


@app.post("/admin/daily")
def admin_daily():
    _check_admin_token()
    summary = run_scraper()
    return jsonify(
        {
            "ok": True,
            "summary": summary,
            "ran_at": datetime.now(LOCAL_TIMEZONE).isoformat(),
        }
    )


@app.get("/admin/daily")
def admin_daily_get():
    _check_admin_token()
    summary = run_scraper()
    return jsonify(
        {
            "ok": True,
            "summary": summary,
            "ran_at": datetime.now(LOCAL_TIMEZONE).isoformat(),
        }
    )


@app.get("/admin/health")
def admin_health():
    return jsonify(
        {
            "ok": True,
            "time": datetime.now(LOCAL_TIMEZONE).isoformat(),
            "odds_api_enabled": odds_api_enabled(),
        }
    )


# ----------------------------------------------------------------------------- #
# UI
# ----------------------------------------------------------------------------- #
@app.route("/")
def index():
    all_predictions = []
    for sport in ["NBA", "MLB", "NFL", "NCAAF"]:
        sport_predictions = predict_game_totals(sport)
        logging.info("%s predictions: %s games", sport, len(sport_predictions))
        all_predictions.extend(sport_predictions)

    all_predictions = sorted(
        all_predictions, key=lambda x: x.get("game_time") or datetime.max
    )
    best_bets = select_best_bets(all_predictions, limit=5)

    local_now = datetime.now(LOCAL_TIMEZONE)
    date_str = local_now.strftime("%B %d, %Y")
    tz_name = local_now.tzname() or "CST"
    logo_url = url_for("static", filename="XHE1qwUp_400x400.jpg")

    return render_template(
        "index.html",
        predictions=all_predictions,
        best_bets=best_bets,
        odds_enabled=odds_api_enabled(),
        date_str=date_str,
        tz_name=tz_name,
        logo_url=logo_url,
    )


@app.route("/robots.txt")
def robots_txt():
    return Response("User-agent: *\nDisallow:", mimetype="text/plain")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "favicon.ico", mimetype="image/x-icon")


if __name__ == "__main__":
    print("Running ESPN scraper manually on startup...")
    run_scraper()
    app.run(host="0.0.0.0", port=5000)
