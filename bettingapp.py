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
    # Fetch data logic (same as before)
    ...

def find_closest_match(user_input, team_list):
    # Fuzzy matching logic (same as before)
    ...

def calculate_predicted(team1, team2, df, sheet_name):
    # Prediction calculation logic (same as before)
    ...

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error_message = None
    sheet_name = None
    teams = []

    # Initialize session history if it doesn't exist
    if "history" not in session:
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
                error_message = "One or both team names were not found. Please try again."
            else:
                result = calculate_predicted(team1_match, team2_match, data, sheet_name)

                # Save input and result to session history
                session["history"].append({
                    "sheet": sheet_name,
                    "team1": team1_match,
                    "team2": team2_match,
                    "result": f"{result:.1f}"
                })
                session.modified = True  # Mark session as modified to save changes
        except Exception as e:
            error_message = f"Error: {e}"

    return render_template(
        "index.html",
        result=result,
        error_message=error_message,
        sheet_name=sheet_name,
        history=session.get("history", [])  # Pass user-specific history
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Use the PORT environment variable
    app.run(host="0.0.0.0", port=port)
