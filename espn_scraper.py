import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# Google Sheets config
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg"

# ESPN API endpoints per league
ESPN_URLS = {
    "MLB":  "https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings",
    "NBA":  "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
    "NFL":  "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
    "NCAAF":"https://site.api.espn.com/apis/v2/sports/football/college-football/standings",
}

# Use relative ranges (no sheet name)
SHEET_RANGES = {
    "MLB":  "A2:D200",
    "NBA":  "A2:D200",
    "NFL":  "A2:D500",
    "NCAAF":"A2:D800",
}

def fetch_espn_standings(league):
    print(f"Fetching ESPN standings JSON for {league}...")
    url = ESPN_URLS[league]
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    teams = []
    for division in data.get("children", []):
        for entry in division.get("standings", {}).get("entries", []):
            team_name = entry.get("team", {}).get("displayName")

            # Extract numeric stats robustly
            stats = {}
            for stat in entry.get("stats", []):
                name = stat.get("name")
                value = stat.get("value") or stat.get("displayValue")
                if name and value not in ("-", "", None):
                    try:
                        stats[name] = float(value)
                    except (ValueError, TypeError):
                        pass

            games_played = stats.get("gamesPlayed", stats.get("wins", 0) + stats.get("losses", 0))
            # Baseball uses runsFor / runsAgainst, others use pointsFor / pointsAgainst
            pf = stats.get("pointsFor") or stats.get("runsFor")
            pa = stats.get("pointsAgainst") or stats.get("runsAgainst")

            if team_name and None not in (games_played, pf, pa):
                row = [team_name, int(games_played), int(pf), int(pa)]
                teams.append(row)
                print(row)

    # Fallback: some responses flatten directly under data["standings"]["entries"]
    if not teams:
        try:
            for entry in data["standings"]["entries"]:
                team_name = entry.get("team", {}).get("displayName")
                stats = {}
                for stat in entry.get("stats", []):
                    name = stat.get("name")
                    value = stat.get("value") or stat.get("displayValue")
                    if name and value not in ("-", "", None):
                        try:
                            stats[name] = float(value)
                        except (ValueError, TypeError):
                            pass
                games_played = stats.get("gamesPlayed", stats.get("wins", 0) + stats.get("losses", 0))
                pf = stats.get("pointsFor") or stats.get("runsFor")
                pa = stats.get("pointsAgainst") or stats.get("runsAgainst")
                if team_name and None not in (games_played, pf, pa):
                    teams.append([team_name, int(games_played), int(pf), int(pa)])
        except Exception:
            pass

    print(f"✅ Retrieved data for {len(teams)} {league} teams.")
    return teams

def update_google_sheet(data, league):
    print(f"Updating Google Sheet for {league}...")
    creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SPREADSHEET_ID)

    # Ensure worksheet exists
    try:
        ws = sh.worksheet(league)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=league, rows="1000", cols="10")

    # Write header you’re using across the app
    ws.update("A1:D1", [["Team", "G", "PF", "PA"]])

    # Clear old body, then write data starting A2 — no sheet name in range
    ws.batch_clear([SHEET_RANGES[league]])
    if data:
        ws.update("A2", data)

    # Timestamp (optional)
    ws.update("F1", [[f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]])
    print("✅ Sheet updated.")

def run_scraper():
    for league in ["MLB", "NBA", "NFL", "NCAAF"]:
        data = fetch_espn_standings(league)
        if data:
            update_google_sheet(data, league)
        else:
            print(f"❌ No data for {league}.")
