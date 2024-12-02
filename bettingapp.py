import os
import logging
from flask import Flask, render_template, request, session
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pandas as pd
from difflib import get_close_matches

# Configure logging
logging.basicConfig(level=logging.ERROR, format="%(asctime)s - %(levelname)s - %(message)s")

# Google Sheets API Scopes
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg"
SHEET_RANGES = {
    "NCAAF": "NCAAF!A1:D135",
    "NBA": "NBA!A1:D31",
    "NFL": "NFL!A1:D135",
}

# Load credentials and token paths from Render's environment variables
CREDENTIALS_PATH = os.getenv("CREDENTIALS_JSON_PATH", "credentials.json")
TOKEN_PATH = os.getenv("TOKEN_JSON_PATH", "token.json")

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your_secret_key")  # Replace with a secure random key in production

# Cache data to avoid multiple API calls
sheet_data_cache = {}

def fetch_data_from_sheets(sheet_name):
    """
    Fetch data from a specific Google Sheet range.
    """
    sheet_name = sheet_name.strip().lower()
    sheet_ranges_normalized = {key.lower(): key for key in SHEET_RANGES}

    if sheet_name not in sheet_ranges_normalized:
        raise ValueError(f"Sheet name '{sheet_name}' is not valid. Please choose from: {', '.join(SHEET_RANGES.keys())}.")

    actual_sheet_name = sheet_ranges_normalized[sheet_name]

    if actual_sheet_name in sheet_data_cache:
        return sheet_data_cache[actual_sheet_name]

    range_ = SHEET_RANGES[actual_sheet_name]

    try:
        credentials = None
        if os.path.exists(TOKEN_PATH):
            credentials = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
                credentials = flow.run_local_server(port=0)
            with open(TOKEN_PATH, "w") as token_file:
                token_file.write(credentials.to_json())

        service = build("sheets", "v4", credentials=credentials)
        sheets = service.spreadsheets()
        result = sheets.values().get(spreadsheetId=SPREADSHEET_ID, range=range_).execute()
        values = result.get("values", [])
        if not values:
            raise ValueError(f"No data found in the '{actual_sheet_name}' sheet.")

        df = pd.DataFrame(values[1:], columns=values[0])
        required_columns = ["Team", "PPG", "OPP PPG"] if actual_sheet_name == "NBA" else ["Team", "G", "PF", "PA"]
        for col in required_columns:
            if col not in df.columns:
                raise ValueError(f"Missing required column '{col}' in the sheet.")

        for col in df.columns:
            if col != "Team":
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        sheet_data_cache[actual_sheet_name] = df.set_index("Team", drop=False)
        return sheet_data_cache[actual_sheet_name]

    except HttpError as error:
        logging.error(f"An API error occurred: {error}")
        raise RuntimeError("Failed to fetch data from Google Sheets.")

def find_closest_match(user_input, team_list):
    user_input = user_input.strip().lower()
    normalized_teams = [team.lower() for team in team_list]

    for team in normalized_teams:
        if user_input in team:
            index = normalized_teams.index(team)
            return team_list[index]

    matches = get_close_matches(user_input, normalized_teams, n=1, cutoff=0.3)
    if matches:
        index = normalized_teams.index(matches[0])
        return team_list[index]
    return None

def calculate_predicted(team1, team2, df, sheet_name):
    team1_stats = df.loc[team1]
    team2_stats = df.loc[team2]

    if sheet_name.lower() == "nba":
        predicted = (team1_stats["PPG"] + team1_stats["OPP PPG"] +
                     team2_stats["PPG"] + team2_stats["OPP PPG"]) / 2
    else:
        team1_avg_for = team1_stats["PF"] / team1_stats["G"]
        team1_avg_against = team1_stats["PA"] / team1_stats["G"]
        team2_avg_for = team2_stats["PF"] / team2_stats["G"]
        team2_avg_against = team2_stats["PA"] / team2_stats["G"]
        predicted = (team1_avg_for + team1_avg_against +
                     team2_avg_for + team2_avg_against) / 2

    return predicted

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error_message = None
    sheet_name = None
    teams = []

    if request.method == "GET":
        session["history"] = []

    if request.method == "POST":
        sheet_name = request.form.get("sheet_name")
        team1 = request.form.get("team1")
        team2 = request.form.get("team2")

        try:
            data = fetch_data_from_sheets(sheet_name)
            teams = data["Team"].unique()
            team1_match = find_closest_match(team1, teams)
            team2_match = find_closest_match(team2, teams)

            if not team1_match or not team2_match:
                closest_matches = [
                    f"Closest match for '{team1}': {find_closest_match(team1, teams) or 'None'}",
                    f"Closest match for '{team2}': {find_closest_match(team2, teams) or 'None'}"
                ]
                error_message = (
                    "One or both team names were not found. Please try again."
                    f" {closest_matches[0]} | {closest_matches[1]}."
                )
            else:
                result = calculate_predicted(team1_match, team2_match, data, sheet_name)
                if "history" not in session:
                    session["history"] = []
                session["history"].append({
                    "sheet": sheet_name,
                    "team1": team1_match,
                    "team2": team2_match,
                    "result": f"{result:.1f}"
                })
                session.modified = True
        except ValueError as ve:
            error_message = str(ve)
        except Exception as e:
            error_message = f"Error: {e}"

    return render_template(
        "index.html",
        result=result,
        error_message=error_message,
        sheet_name=sheet_name,
        history=session.get("history", [])
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
