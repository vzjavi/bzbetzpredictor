import os
import logging
import json
import requests
import pandas as pd
from flask import Flask, render_template
from google.oauth2.service_account import Credentials
from google.oauth2.service_account import Credentials
from flask import Flask, render_template, request, session, jsonify
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime
from difflib import get_close_matches
import pytz

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
    "MLB": "4424"
}

sheet_data_cache = {}
LOCAL_TIMEZONE = pytz.timezone("America/New_York")

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

def fetch_data_from_sheets(sheet_name):
    if sheet_name in sheet_data_cache:
        return sheet_data_cache[sheet_name]

    range_ = SHEET_RANGES.get(sheet_name)
    if not range_:
        raise ValueError("Invalid sheet name.")

    try:
        with open("service_account.json") as f:
            creds_dict = json.load(f)
        creds = Credentials.from_service_account_info(creds_dict)
        scoped = creds.with_scopes(['https://www.googleapis.com/auth/spreadsheets.readonly'])

        service = build("sheets", "v4", credentials=scoped)
        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=range_).execute()
        values = result.get("values", [])
        if not values:
            raise ValueError("No data found in the sheet.")

        df = pd.DataFrame(values[1:], columns=values[0])
        numeric_columns = ["PPG", "OPP PPG"] if sheet_name == "NBA" else ["G", "PF", "PA"]
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        sheet_data_cache[sheet_name] = df.set_index("Team", drop=False)
        return sheet_data_cache[sheet_name]

    except HttpError as error:
        logging.error(f"An API error occurred: {error}")
        raise RuntimeError("Failed to fetch data from Google Sheets.")

def get_todays_games(league_name):
    league_id = SPORT_LEAGUES[league_name]
    today = datetime.now().strftime("%Y-%m-%d")
    url = f"https://www.thesportsdb.com/api/v1/json/{API_KEY}/eventsday.php?d={today}&l={league_id}"
    try:
        response = requests.get(url)
        data = response.json()
        return data.get("events", [])
    except Exception as e:
        logging.error(f"Error fetching {league_name} games: {e}")
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
    games = get_todays_games(league_name)
    stats_df = fetch_data_from_sheets(league_name)
    team_list = stats_df.index.tolist()
    seen_matchups = set()

    for game in games:
        team1_api = game.get("strHomeTeam")
        team2_api = game.get("strAwayTeam")

        # Parse and convert time to local timezone
        game_time_utc = None
        if game.get("dateEvent") and game.get("strTime"):
            dt_str = f"{game['dateEvent']} {game['strTime']}"
            try:
                utc_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
                game_time_utc = utc_time.astimezone(LOCAL_TIMEZONE)
            except Exception as e:
                logging.warning(f"Could not parse or convert time: {dt_str} — {e}")

        team1 = find_team_match(team1_api, team_list)
        team2 = find_team_match(team2_api, team_list)

        if not team1 or not team2:
            logging.warning(f"Skipping: {team1_api} vs {team2_api} — unmatched")
            continue

        matchup_key = tuple(sorted([team1, team2]))
        if matchup_key in seen_matchups:
            continue
        seen_matchups.add(matchup_key)

        row1 = stats_df.loc[team1]
        row2 = stats_df.loc[team2]

        if league_name == "NBA":
            total = round((row1["PPG"] + row1["OPP PPG"] + row2["PPG"] + row2["OPP PPG"]) / 2, 1)
        else:
            total = round(((row1["PF"] / row1["G"] + row1["PA"] / row1["G"] +
                            row2["PF"] / row2["G"] + row2["PA"] / row2["G"]) / 2), 1)

        predictions.append({
            "sport": league_name,
            "team1": team1,
            "team2": team2,
            "team1_logo": get_team_logo(team1, league_name),
            "team2_logo": get_team_logo(team2, league_name),
            "predicted_total": total,
            "game_time": game_time_utc
        })

    return predictions

@app.route("/")
def index():
    all_predictions = []
    for sport in SPORT_LEAGUES:
        all_predictions.extend(predict_game_totals(sport))

    all_predictions = sorted(all_predictions, key=lambda x: (x.get("game_time") or datetime.max))
    today = datetime.now()
    return render_template("index.html", predictions=all_predictions, now=today)

if __name__ == "__main__":
    app.run(debug=True)