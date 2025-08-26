import os
import logging
import json
import requests
import pandas as pd
from flask import Flask, render_template, request, session, jsonify, abort
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime
from difflib import get_close_matches
from espn_scraper import run_scraper
import pytz
from ncaaf_team_matching_helper import extend_mapping_with_schedule, resolve_team, save_mapping

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

sheet_data_cache = {}
LOCAL_TIMEZONE = pytz.timezone("America/Chicago")

NCAAF_MAP_PATH = os.path.join(os.path.dirname(__file__), "ncaaf_team_mapping.json")

try:
    with open(NCAAF_MAP_PATH, "r", encoding="utf-8") as f:
        ncaaf_map = json.load(f)
except FileNotFoundError:
    ncaaf_map = {}

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

def fetch_data_from_sheets(league_tab: str) -> pd.DataFrame:
    """
    Loads Team, G, PF, PA from the Google Sheet tab (A1:D).
    Returns a DataFrame indexed by Team with numeric G/PF/PA.
    """
    SHEET_ID = os.environ.get("GOOGLE_SHEETS_ID", "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg")
    creds_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        os.path.join(os.path.dirname(__file__), "service_account.json"),
    )
    creds = Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)

    rng = f"{league_tab}!A1:D1000"
    res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=rng).execute()
    values = res.get("values", [])

    if not values or len(values) < 2:
        return pd.DataFrame(columns=["G", "PF", "PA"], index=pd.Index([], name="Team"))

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
            # Use correct keys from TheSportsDB payload
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

    def find_row(team_name):
        # For NCAAF, resolve team -> stats_key before matching
        if league_name == "NCAAF":
            try:
                _, stats_key = resolve_team(team_name, ncaaf_map)
                team_name = stats_key
            except Exception as e:
                logging.warning(f"NCAAF resolve failed for {team_name}: {e}")
        match = find_team_match(team_name, team_list)
        if not match:
            return None, None
        try:
            return match, stats_df.loc[match]
        except KeyError:
            return None, None

    def per_game(row):
        try:
            g = float(row.get("G", 0)) or 0.0
            pf = float(row.get("PF", 0)) or 0.0
            pa = float(row.get("PA", 0)) or 0.0
            if g <= 0:
                return 0.0, 0.0
            return pf / g, pa / g
        except Exception:
            return 0.0, 0.0

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

        pfpg1, papg1 = per_game(row1)
        pfpg2, papg2 = per_game(row2)
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
