import os
import logging
import json
import requests
import pandas as pd
from flask import Flask, render_template, request, session, jsonify, abort, Response, send_from_directory
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime, timedelta
from difflib import get_close_matches
from espn_scraper import run_scraper
import pytz
from ncaaf_team_matching_helper import extend_mapping_with_schedule, resolve_team, save_mapping
import re

# Logging config
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Load logo data from local JSON
with open("team_logos.json", "r") as f:
    logo_data = json.load(f)

# Flask app setup
app = Flask(__name__)
app.secret_key = "your_secret_key"

# Constants
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg"

SHEET_RANGES = {
    "NCAAF": "NCAAF!A1:D135",
    "NBA": "NBA!A1:D31",
    "NFL": "NFL!A1:D135",
    "MLB": "MLB!A1:D31",
}

API_KEY = "697039"
SPORT_LEAGUES = {
    "NBA": "4387",
    "MLB": "4424",
    "NCAAF": "4479",
    "NFL": "4391"
}

# Simple cache for Sheets reads to avoid 429s (per-minute rate limit)
sheet_data_cache = {}  # {league_tab: (fetched_at_datetime, DataFrame)}
CACHE_TTL = timedelta(minutes=2)

LOCAL_TIMEZONE = pytz.timezone("America/Chicago")

NCAAF_MAP_PATH = os.path.join(os.path.dirname(__file__), "ncaaf_team_mapping.json")

try:
    with open(NCAAF_MAP_PATH, "r", encoding="utf-8") as f:
        ncaaf_map = json.load(f)
except FileNotFoundError:
    ncaaf_map = {}

# Aliases used for logo lookups (NBA / MLB here). College handled separately below.
TEAM_ALIASES = {
    "Philadelphia 76ers": "76ers",
    "Milwaukee Bucks": "Bucks",
    "Chicago Bulls": "Bulls",
    "Cleveland Cavaliers": "Cavaliers",
    "Boston Celtics": "Celtics",
    "Los Angeles Clippers": "Clippers",
    "Memphis Grizzlies": "Grizzlies",
    "Atlanta Hawks": "Hawks",
    "Miami Heat": "Heat",
    "Charlotte Hornets": "Hornets",
    "Utah Jazz": "Jazz",
    "Sacramento Kings": "Kings",
    "New York Knicks": "Knicks",
    "Los Angeles Lakers": "Lakers",
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
    "New York Yankees": "Yankees"
}

# ---- NCAAF name normalization helpers ---------------------------------------

# High-impact short names & variants -> school name as it likely appears in Sheets
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
    "Long Island": "LIU",        # sometimes Sheets use LIU, sometimes "Long Island"
    "LIU": "Long Island",
}

# scrubs punctuation & the “University of …” style noise
def _normalize_college_name(name: str) -> str:
    s = name.strip()
    if s in NCAAF_NAME_ALIASES:
        return NCAAF_NAME_ALIASES[s]

    # Replace “&” variations and apostrophes
    s = s.replace("&", "and")
    s = s.replace("’", "'").replace("ʻ", "'").replace("`", "'")
    s = s.replace("'", "")

    # Drop common boilerplate
    s = re.sub(r"^(University of|Univ\. of|Univ of)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+University$", "", s, flags=re.I)

    # Expand “St.” to “State” and vice versa (for fuzzy attempts)
    s = re.sub(r"\bSt\.?\b", "State", s, flags=re.I)

    return s.strip()


def _ncaaf_variants(name: str):
    """Generate a few reasonable variants to try against the Sheet index."""
    base = name.strip()
    norm = _normalize_college_name(base)
    variants = {base, norm}

    # also try the alias mapping both ways if present
    for k, v in NCAAF_NAME_ALIASES.items():
        if base == k:
            variants.add(v)
        if base == v:
            variants.add(k)

    # if “State” present, try St, and if mascot attached, try without
    if "State" in norm:
        variants.add(norm.replace("State", "St"))
    if "St " in norm:
        variants.add(norm.replace("St", "State"))

    # strip mascots (simple rule: remove last token if list is 3+ and looks like mascot)
    tokens = norm.split()
    if len(tokens) >= 3:
        variants.add(" ".join(tokens[:2]))  # “Ohio State Buckeyes” -> “Ohio State”

    return [v for v in variants if v]


def fetch_data_from_sheets(league_tab: str) -> pd.DataFrame:
    """
    Loads Team, G, PF, PA from the Google Sheet tab (A1:D).
    Returns a DataFrame indexed by Team with numeric G/PF/PA.
    Uses a small in-memory cache to respect Sheets API quotas.
    """
    # Cache check
    now = datetime.utcnow()
    cached = sheet_data_cache.get(league_tab)
    if cached:
        fetched_at, df_cached = cached
        if now - fetched_at < CACHE_TTL:
            return df_cached.copy()

    SHEET_ID = os.environ.get("GOOGLE_SHEETS_ID", "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg")
    creds_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        os.path.join(os.path.dirname(__file__), "service_account.json"),
    )
    creds = Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    # ADD cache_discovery=False to prevent the oauth2client file_cache warning
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    rng = f"{league_tab}!A1:D1000"
    res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=rng).execute()
    values = res.get("values", [])

    if not values or len(values) < 2:
        df_empty = pd.DataFrame(columns=["G", "PF", "PA"], index=pd.Index([], name="Team"))
        sheet_data_cache[league_tab] = (now, df_empty.copy())
        return df_empty

    # Force header to exactly Team,G,PF,PA (ignore any extra columns in the sheet)
    expected = ["Team", "G", "PF", "PA"]
    header = (values[0] + ["", "", "", ""])[:4]
    # Normalize body rows to 4 elements
    rows = []
    for r in values[1:]:
        if isinstance(r, str):
            r = [r]
        if not isinstance(r, list):
            r = [str(r)]
        r = (r + ["", "", "", ""])[:4]
        rows.append(r)

    df = pd.DataFrame(rows, columns=expected)

    # Drop blank teams
    df["Team"] = df["Team"].astype(str).str.strip()
    df = df[df["Team"] != ""]
    # Coerce numeric
    for c in ["G", "PF", "PA"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    df.set_index("Team", inplace=True)

    # Save to cache
    sheet_data_cache[league_tab] = (now, df.copy())
    return df


def get_todays_games(league_name):
    league_id = SPORT_LEAGUES[league_name]
    today = datetime.now(LOCAL_TIMEZONE).date()
    season_map = {
        "NBA": "2025-2026",
        "MLB": "2025",
        "NFL": "2025",
        "NCAAF": "2025"
    }

    season = season_map.get(league_name, "2024")
    url = f"https://www.thesportsdb.com/api/v1/json/{API_KEY}/eventsseason.php?id={league_id}&s={season}"
    try:
        response = requests.get(url)
        data = response.json()
        raw_events = data.get("events")
        if raw_events is None:
            logging.warning(f"No events returned from API for {league_name} — check season and league ID")
            raw_events = []

        def is_game_today(game):
            if not game.get("dateEvent") or not game.get("strTime"):
                return False
            dt_str = f"{game['dateEvent']} {game['strTime']}"
            try:
                game_dt_utc = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
                game_dt_cst = game_dt_utc.astimezone(LOCAL_TIMEZONE)
                return game_dt_cst.date() == today
            except Exception as e:
                logging.warning(f"Could not parse game datetime: {dt_str}, error: {e}")
                return False

        return [game for game in raw_events if is_game_today(game)]
    except Exception as e:
        logging.error(f"Error fetching {league_name} season games: {e}")
        return []


def find_team_match(team_name, team_list):
    team_name = team_name.strip()
    if team_name in team_list:
        return team_name
    if team_name in TEAM_ALIASES:
        alias_target = TEAM_ALIASES[team_name]
        if alias_target in team_list:
            return alias_target
    # fuzzy
    matches = get_close_matches(team_name, team_list, n=1, cutoff=0.5)
    if matches:
        return matches[0]
    logging.warning(f"⚠️ No match found for team: {team_name}")
    return None


def get_team_logo(team_short_name, sport):
    full_name = next((k for k, v in TEAM_ALIASES.items() if v == team_short_name), team_short_name)
    try:
        return logo_data.get(sport, {}).get(full_name, None)
    except Exception as e:
        logging.warning(f"Logo lookup failed for {team_short_name}: {e}")
        return None


def predict_game_totals(league_name):
    predictions = []

    # Get today's games from TheSportsDB
    games = get_todays_games(league_name)
    logging.info(f"{league_name} games fetched: {len(games)}")

    # NCAAF: extend team map with any new schedule teams
    if league_name == "NCAAF":
        global ncaaf_map
        schedule_teams = set()
        for g in games:
            schedule_teams.add(g.get("strHomeTeam"))
            schedule_teams.add(g.get("strAwayTeam"))
        schedule_teams = {t for t in schedule_teams if t}
        try:
            ncaaf_map = extend_mapping_with_schedule(schedule_teams, ncaaf_map, sheet_names=None)
            save_mapping(NCAAF_MAP_PATH, ncaaf_map)
        except Exception as e:
            logging.warning(f"NCAAF mapping extend failed: {e}")

    # Load stats from Google Sheets (expects columns: Team | G | PF | PA)
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

    

    def _find_row_ncaaf(raw_name):
        """
        Robust NCAAF matcher (resolver-first):
        1) Ask resolve_team(...) to choose the right school (uses mapping + guardrails).
        Try its stats_key and primary against the Sheet.
        2) If that fails, try direct/fuzzy against the Sheet with normalized variants.
        """

        # (1) resolver FIRST (lets Miami guardrail take effect)
        try:
            primary, stats_key = resolve_team(raw_name, ncaaf_map, sheet_names=team_list)
            logging.info(f"[NCAAF resolver] raw='{raw_name}' → primary='{primary}', stats_key='{stats_key}'")
            # Try stats_key first (that's how your Sheet is keyed), then primary
            for cand in [stats_key, primary]:
                if cand:
                    match = find_team_match(cand, team_list)
                    if match:
                        try:
                            return match, stats_df.loc[match]
                        except KeyError:
                            pass
        except Exception as e:
            logging.warning(f"resolve_team failed for '{raw_name}': {e}")

    # (2) fall back to raw + normalized variants straight against Sheet names
    for cand in _ncaaf_variants(raw_name):
        match = find_team_match(cand, team_list)
        if match:
            try:
                return match, stats_df.loc[match]
            except KeyError:
                pass

    logging.warning(f"⚠️ No match found for college team: {raw_name}")
    return None, None


    def find_row(team_name):
        if league_name == "NCAAF":
            return _find_row_ncaaf(team_name)
        # Non-college: use generic matching (handles MLB/NBA/NFL fine)
        match = find_team_match(team_name, team_list)
        if not match:
            return None, None
        try:
            return match, stats_df.loc[match]
        except KeyError:
            return None, None

    for game in games:
        team_home = game.get("strHomeTeam")
        team_away = game.get("strAwayTeam")

        # Parse & localize time
        game_time_local = None
        if game.get("dateEvent") and game.get("strTime"):
            dt_str = f"{game['dateEvent']} {game['strTime']}"
            try:
                utc_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
                game_time_local = utc_time.astimezone(LOCAL_TIMEZONE)
            except Exception as e:
                logging.warning(f"Could not parse/convert time: {dt_str} — {e}")

        name1, row1 = find_row(team_home)
        name2, row2 = find_row(team_away)

        if not name1 or not name2:
            logging.warning(f"Skipping: {team_home} vs {team_away} — unmatched")
            continue

        matchup_key = tuple(sorted([name1, name2]))
        if matchup_key in seen_matchups:
            continue
        seen_matchups.add(matchup_key)

        pfpg1, papg1 = _per_game(row1)
        pfpg2, papg2 = _per_game(row2)
        predicted_total = round(((pfpg1 + papg1 + pfpg2 + papg2) / 2.0), 1)

        predictions.append({
            "sport": league_name,
            "team1": name1,
            "team2": name2,
            "team1_logo": get_team_logo(name1, league_name),
            "team2_logo": get_team_logo(name2, league_name),
            "predicted_total": predicted_total,
            "game_time": game_time_local
        })

    return predictions

# -------------------- Admin endpoints for cron/ops ----------------------------

def _check_admin_token():
    expected = os.environ.get("ADMIN_TOKEN", "")
    got = request.headers.get("X-Admin-Token", "")
    if not expected or got != expected:
        abort(401)

@app.post("/admin/daily")
def admin_daily():
    """
    Trigger daily maintenance tasks (cron-safe).
    Currently: run the ESPN scraper to refresh Sheets.
    """
    _check_admin_token()
    summary = run_scraper()
    return jsonify({"ok": True, "summary": summary, "ran_at": datetime.now(LOCAL_TIMEZONE).isoformat()})

# (Optional) GET variant for manual testing
@app.get("/admin/daily")
def admin_daily_get():
    _check_admin_token()
    summary = run_scraper()
    return jsonify({"ok": True, "summary": summary, "ran_at": datetime.now(LOCAL_TIMEZONE).isoformat()})

# (Optional) health check
@app.get("/admin/health")
def admin_health():
    return jsonify({"ok": True, "time": datetime.now(LOCAL_TIMEZONE).isoformat()})

# -------------------- Web UI --------------------------------------------------

@app.route("/")
def index():
    all_predictions = []
    for sport in ["NBA", "MLB", "NFL", "NCAAF"]:
        sport_predictions = predict_game_totals(sport)
        logging.info(f"{sport} predictions: {len(sport_predictions)} games")
        all_predictions.extend(sport_predictions)

    all_predictions = sorted(all_predictions, key=lambda x: x.get("game_time") or datetime.max)
    return render_template("index.html", predictions=all_predictions, now=datetime.now(LOCAL_TIMEZONE))


if __name__ == "__main__":
    print("📊 Running ESPN scraper manually on startup...")
    run_scraper()
    app.run(host="0.0.0.0", port=5000)

# --- Minimal robots.txt and favicon routes ---
@app.route("/robots.txt")
def robots_txt():
    return Response("User-agent: *\nDisallow:", mimetype="text/plain")

@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "favicon.ico", mimetype="image/x-icon")
