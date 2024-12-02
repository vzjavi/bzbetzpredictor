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
import json

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
    # Normalize sheet_name to lowercase
    sheet_name = sheet_name.strip().lower()
    
    # Create a case-insensitive lookup for SHEET_RANGES
    sheet_ranges_normalized = {key.lower(): key for key in SHEET_RANGES}

    # Match user input with the correct sheet name
    if sheet_name not in sheet_ranges_normalized:
        raise ValueError(f"Sheet name '{sheet_name}' is not valid. Please choose from: {', '.join(SHEET_RANGES.keys())}.")

    # Use the normalized key to fetch the actual sheet name
    actual_sheet_name = sheet_ranges_normalized[sheet_name]

    if actual_sheet_name in sheet_data_cache:
        return sheet_data_cache[actual_sheet_name]

    # Fetch fresh data
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

        # Fetch data
        service = build("sheets", "v4", credentials=credentials)
        sheets = service.spreadsheets()
        result = sheets.values().get(spreadsheetId=SPREADSHEET_ID, range=range_).execute()
        values = result.get("values", [])
        if not values:
            raise ValueError(f"No data found in the '{actual_sheet_name}' sheet.")

        # Convert to DataFrame
        df = pd.DataFrame(values[1:], columns=values[0])  # Assume first row is header
        required_columns = ["Team", "G", "PF", "PA"] if actual_sheet_name != "NBA" else ["Team", "PPG", "OPP PPG"]
        for col in required_columns:
            if col not in df.columns:
                raise ValueError(f"Missing required column '{col}' in the sheet.")

        # Convert columns to numeric where applicable
        for col in df.columns:
            if col != "Team":  # Skip "Team" column
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        # Cache data
        sheet_data_cache[actual_sheet_name] = df.set_index("Team", drop=False)
        return sheet_data_cache[actual_sheet_name]

    except HttpError as error:
        logging.error(f"An API error occurred: {error}")
        raise RuntimeError("Failed to fetch data from Google Sheets.")

    # Authentication
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

    try:
        # Fetch data
        service = build("sheets", "v4", credentials=credentials)
        sheets = service.spreadsheets()
        result = sheets.values().get(spreadsheetId=SPREADSHEET_ID, range=range_).execute()
        values = result.get("values", [])
        if not values:
            raise ValueError(f"No data found in the '{sheet_name}' sheet.")
        
        # Convert to DataFrame
        df = pd.DataFrame(values[1:], columns=values[0])  # Assume first row is header
        required_columns = ["Team", "G", "PF", "PA"] if sheet_name != "NBA" else ["Team", "PPG", "OPP PPG"]
        for col in required_columns:
            if col not in df.columns:
                raise ValueError(f"Missing required column '{col}' in the sheet.")
        
        # Convert columns to numeric where applicable
        for col in df.columns:
            if col != "Team":  # Skip "Team" column
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        
        # Cache data
        sheet_data_cache[sheet_name] = df.set_index("Team", drop=False)
        return sheet_data_cache[sheet_name]
    except HttpError as error:
        logging.error(f"An API error occurred: {error}")
        raise RuntimeError("Failed to fetch data from Google Sheets.")

def find_closest_match(user_input, team_list):
    """
    Find the closest team name match to the user's input using fuzzy matching.
    """
    matches = get_close_matches(user_input, team_list, n=1, cutoff=0.6)
    return matches[0] if matches else None

def calculate_predicted(team1, team2, df, sheet_name):
    """
    Generalized function to calculate predicted over/under score.
    """
    team1_stats = df.loc[team1]
    team2_stats = df.loc[team2]

    if sheet_name.lower() == "nba":
        # Use NBA-specific columns
        predicted = (team1_stats["PPG"] + team1_stats["OPP PPG"] +
                     team2_stats["PPG"] + team2_stats["OPP PPG"]) / 2
    else:
        # Use columns for other sports
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

    # Clear history if the user refreshes the page (GET request)
    if request.method == "GET":
        session["history"] = []

    if request.method == "POST":
        sheet_name = request.form.get("sheet_name")
        team1 = request.form.get("team1")
        team2 = request.form.get("team2")

        try:
            # Fetch data
            data = fetch_data_from_sheets(sheet_name)
            teams = data["Team"].unique()
            
            # Find closest matches for team names
            team1_match = find_closest_match(team1, teams)
            team2_match = find_closest_match(team2, teams)

            if not team1_match or not team2_match:
                error_message = "One or both team names were not found. Please try again."
            else:
                # Calculate prediction
                result = calculate_predicted(team1_match, team2_match, data, sheet_name)

                # Save input and result to session history
                if "history" not in session:
                    session["history"] = []
                session["history"].append({
                    "sheet": sheet_name,
                    "team1": team1_match,
                    "team2": team2_match,
                    "result": f"{result:.1f}"
                })
                session.modified = True  # Mark session as modified
        except ValueError as ve:
            error_message = str(ve)
        except Exception as e:
            error_message = f"Error: {e}"

    return render_template(
        "index.html",
        result=result,
        error_message=error_message,
        sheet_name=sheet_name,
        history=session.get("history", [])  # Pass user's history
    )




if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Use the PORT environment variable
    app.run(host="0.0.0.0", port=port)
