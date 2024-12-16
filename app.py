import os
import logging
from flask import Flask, render_template, request, session, jsonify
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pandas as pd
from difflib import get_close_matches

# Configure logging
logging.basicConfig(level=logging.ERROR, format="%(asctime)s - %(levelname)s - %(message)s")

# Google Sheets API Scopes and Configuration
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg"
SHEET_RANGES = {
    "NCAAF": "NCAAF!A1:D135",
    "NBA": "NBA!A1:D31",
    "NFL": "NFL!A1:D135",
}

CREDENTIALS_PATH = "credentials.json"
TOKEN_PATH = "token.json"

# Initialize Flask app
app = Flask(__name__)
app.secret_key = "your_secret_key"  # Replace with a secure key in production
sheet_data_cache = {}

def fetch_data_from_sheets(sheet_name):
    """
    Fetches data from Google Sheets and caches it.
    """
    if sheet_name in sheet_data_cache:
        return sheet_data_cache[sheet_name]

    range_ = SHEET_RANGES.get(sheet_name)
    if not range_:
        raise ValueError("Invalid sheet name.")

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
        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=range_).execute()
        values = result.get("values", [])
        if not values:
            raise ValueError("No data found in the sheet.")

        # Convert data to DataFrame
        df = pd.DataFrame(values[1:], columns=values[0])

        # Convert numeric columns to proper types
        numeric_columns = ["PPG", "OPP PPG"] if sheet_name == "NBA" else ["G", "PF", "PA"]
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        sheet_data_cache[sheet_name] = df.set_index("Team", drop=False)
        return sheet_data_cache[sheet_name]

    except HttpError as error:
        logging.error(f"An API error occurred: {error}")
        raise RuntimeError("Failed to fetch data from Google Sheets.")


def find_closest_match(user_input, team_list):
    """
    Find the closest matching team name using fuzzy matching.
    """
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

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error_message = None
    selected_team1 = None
    selected_team2 = None

    # Clear session history when the user refreshes the page (GET request)
    if request.method == "GET":
        session.pop("history", None)  # Remove 'history' from session

    if request.method == "POST":
        sheet_name = request.form.get("sheet_name")
        team1 = request.form.get("team1")
        team2 = request.form.get("team2")

        try:
            # Fetch the correct sheet data
            data = fetch_data_from_sheets(sheet_name)
            teams = data["Team"].tolist()

            # Find closest matches for the teams
            selected_team1 = find_closest_match(team1, teams)
            selected_team2 = find_closest_match(team2, teams)

            if not selected_team1 or not selected_team2:
                error_message = "One or both team names not found."
            else:
                # Over/Under calculation logic
                if sheet_name == "NBA":
                    result = round((data.loc[selected_team1, "PPG"] +
                                    data.loc[selected_team1, "OPP PPG"] +
                                    data.loc[selected_team2, "PPG"] +
                                    data.loc[selected_team2, "OPP PPG"]) / 2, 1)
                else:
                    result = round((data.loc[selected_team1, "PF"] / data.loc[selected_team1, "G"] +
                                    data.loc[selected_team1, "PA"] / data.loc[selected_team1, "G"] +
                                    data.loc[selected_team2, "PF"] / data.loc[selected_team2, "G"] +
                                    data.loc[selected_team2, "PA"] / data.loc[selected_team2, "G"]) / 2, 1)

                # Save the result and matchup to session history
                if "history" not in session:
                    session["history"] = []
                session["history"].append({
                    "sport": sheet_name,
                    "team1": selected_team1,
                    "team2": selected_team2,
                    "result": result
                })
                session.modified = True  # Mark session as modified

        except Exception as e:
            error_message = f"Error occurred: {str(e)}"

    return render_template(
        "index.html", 
        result=result, 
        error_message=error_message, 
        history=session.get("history", []),
        selected_team1=selected_team1,
        selected_team2=selected_team2
    )




@app.route('/get-teams', methods=['GET'])
def get_teams():
    try:
        all_teams = {}  # Dictionary to hold teams categorized by sheet name

        # Iterate through all sheet names in SHEET_RANGES
        for sheet_name, sheet_range in SHEET_RANGES.items():
            data = fetch_data_from_sheets(sheet_name)  # Fetch sheet data
            teams = data["Team"].tolist()  # Extract the "Team" column
            all_teams[sheet_name] = teams  # Store under the sheet name

        # Log for debugging
        print("Fetched Teams from All Sheets:", all_teams)

        return jsonify(all_teams)  # Return the categorized teams
    except Exception as e:
        logging.error(f"Error fetching teams: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
