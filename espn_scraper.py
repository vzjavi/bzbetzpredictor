import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# Google Sheets config
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg"
RANGE_NAME = "MLB!A2:D32"

def fetch_espn_standings():
    print("Fetching ESPN standings JSON...")
    url = "https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings"
    response = requests.get(url)
    data = response.json()

    teams = []
    for division in data['children']:
        for entry in division['standings']['entries']:
            team_name = entry['team']['displayName']

            # Extract stats with fallback to displayValue
            stats = {}
            for stat in entry['stats']:
                name = stat.get('name')
                value = stat.get('value') or stat.get('displayValue')
                if name and value not in ("-", ""):
                    try:
                        stats[name] = float(value)
                    except ValueError:
                        pass

            games_played = stats.get("gamesPlayed", stats.get("wins", 0) + stats.get("losses", 0))
            pf = stats.get("pointsFor") or stats.get("runsFor")
            pa = stats.get("pointsAgainst") or stats.get("runsAgainst")

            if None not in (games_played, pf, pa):
                row = [team_name, int(games_played), int(pf), int(pa)]
                teams.append(row)
                print(row)

    print(f"✅ Retrieved data for {len(teams)} teams.")
    return teams



def update_google_sheet(data):
    print("Updating Google Sheet...")
    creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID)
    worksheet = sheet.worksheet("MLB")

    # Correct usage
    worksheet.update(range_name="A2:D32", values=data)

    # Optional timestamp
    worksheet.update(range_name="F1", values=[[f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]])
    print("✅ Sheet updated.")


def run_scraper():
    data = fetch_espn_standings()
    if data:
        update_google_sheet(data)
    else:
        print("❌ No data to update.")

