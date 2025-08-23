import os
import json
import logging
from datetime import datetime

import requests
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = os.environ.get("GOOGLE_SHEETS_ID", "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg")

ESPN_URLS = {
    "MLB":  "https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings",
    "NBA":  "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
    "NFL":  "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
    "NCAAF":"https://site.api.espn.com/apis/v2/sports/football/college-football/standings",
}

# These are the row ranges we write into (inside each worksheet)
BODY_RANGE = "A2:D1000"
HEADER_RANGE = "A1:D1"

def _load_credentials() -> Credentials:
    # env JSON
    blob = os.environ.get("GOOGLE_CREDS_JSON")
    if blob:
        return Credentials.from_service_account_info(json.loads(blob), scopes=SCOPES)

    # secret file
    secret = "/etc/secrets/service_account.json"
    if os.path.exists(secret):
        return Credentials.from_service_account_file(secret, scopes=SCOPES)

    # env path or local fallback
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac and os.path.exists(gac):
        return Credentials.from_service_account_file(gac, scopes=SCOPES)

    local = os.path.join(os.path.dirname(__file__), "service_account.json")
    if os.path.exists(local):
        return Credentials.from_service_account_file(local, scopes=SCOPES)

    raise RuntimeError("No Google credentials found for espn_scraper.")

def fetch_espn_standings(league: str):
    logging.info(f"Fetching ESPN standings JSON for {league}...")
    url = ESPN_URLS[league]
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    teams = []

    # Primary path: children[*].standings.entries[*]
    for child in data.get("children", []):
        for entry in (child.get("standings") or {}).get("entries", []):
            _extract(entry, teams)

    # Fallback if top-level flattening
    if not teams:
        for entry in (data.get("standings") or {}).get("entries", []):
            _extract(entry, teams)

    logging.info(f"✅ Retrieved data for {len(teams)} {league} teams.")
    return teams

def _extract(entry, out_list):
    name = (entry.get("team") or {}).get("displayName")
    stats = {}
    for s in entry.get("stats", []):
        k = s.get("name")
        v = s.get("value") if s.get("value") not in (None, "-", "") else s.get("displayValue")
        if k and v not in (None, "-", ""):
            try:
                stats[k] = float(v)
            except Exception:
                pass

    # gamesPlayed fallback for some sports
    g = stats.get("gamesPlayed", stats.get("wins", 0) + stats.get("losses", 0) + stats.get("ties", 0))

    # Baseball uses runs*, others points*
    pf = stats.get("pointsFor") or stats.get("runsFor")
    pa = stats.get("pointsAgainst") or stats.get("runsAgainst")

    if name and pf is not None and pa is not None:
        row = [name, int(g or 0), int(pf), int(pa)]
        out_list.append(row)
        logging.info(str(row))

def _open_worksheet(sh, title: str):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="1000", cols="10")

def update_google_sheet(league: str, rows):
    logging.info(f"Updating Google Sheet for {league}...")
    creds = _load_credentials()
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)

    ws = _open_worksheet(sh, league)
    ws.update(HEADER_RANGE, [["Team", "G", "PF", "PA"]])
    ws.batch_clear([BODY_RANGE])
    if rows:
        ws.update("A2", rows)
    ws.update("F1", [[f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]])
    logging.info("✅ Sheet updated.")

def run_scraper():
    """Pull ESPN standings for each league and write to the corresponding tab."""
    summary = {}
    for lg in ["MLB", "NBA", "NFL", "NCAAF"]:
        try:
            data = fetch_espn_standings(lg)
            summary[lg] = len(data)
            update_google_sheet(lg, data)
        except Exception as e:
            logging.exception(f"❌ {lg} scraping failed: {e}")
            summary[lg] = 0
    return summary

if __name__ == "__main__":
    run_scraper()
