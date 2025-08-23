import os
import json
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Set

import requests
from requests.adapters import HTTPAdapter, Retry
import gspread
from google.oauth2.service_account import Credentials
import pytz

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = os.environ.get("GOOGLE_SHEETS_ID", "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg")

ESPN_URLS = {
    "MLB":  "https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings",
    "NBA":  "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
    "NFL":  "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
    "NCAAF":"https://site.api.espn.com/apis/v2/sports/football/college-football/standings",
}

# Ranges we write into (per worksheet)
BODY_RANGE = "A2:D1000"
HEADER_RANGE = "A1:D1"

# -----------------------------------------------------------------------------
# Google Auth
# -----------------------------------------------------------------------------
def _load_credentials() -> Credentials:
    # Inline JSON via env
    blob = os.environ.get("GOOGLE_CREDS_JSON")
    if blob:
        return Credentials.from_service_account_info(json.loads(blob), scopes=SCOPES)

    # Secret file (k8s/Docker style)
    secret = "/etc/secrets/service_account.json"
    if os.path.exists(secret):
        return Credentials.from_service_account_file(secret, scopes=SCOPES)

    # Standard env var path
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac and os.path.exists(gac):
        return Credentials.from_service_account_file(gac, scopes=SCOPES)

    # Local fallback next to script
    local = os.path.join(os.path.dirname(__file__), "service_account.json")
    if os.path.exists(local):
        return Credentials.from_service_account_file(local, scopes=SCOPES)

    raise RuntimeError("No Google credentials found for espn_scraper.")

# -----------------------------------------------------------------------------
# HTTP session (retries + UA)
# -----------------------------------------------------------------------------
def _http() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "bzbetz-predictor/1.0 (+https://example.com)"
    })
    retries = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"])
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def _first(d: Dict[str, Any], *keys, default=None):
    """Return first present value from dict using any of the given keys."""
    for k in keys:
        if k in d and d[k] not in (None, "-", ""):
            return d[k]
    return default

def _to_int(x, default=0):
    try:
        return int(float(x))
    except Exception:
        return default

# -----------------------------------------------------------------------------
# ESPN parsing
# -----------------------------------------------------------------------------
def _extract(entry: Dict[str, Any], out_list: List[List[Any]], seen: Set[str]) -> None:
    """Extract a single team row robustly across ESPN variants."""
    team = (entry.get("team") or {})
    name = team.get("displayName") or team.get("shortDisplayName") or team.get("name")
    if not name:
        return

    # Avoid duplicates when same team appears in multiple conference 'children'
    key = name.strip().lower()
    if key in seen:
        return
    seen.add(key)

    # Build a stats dict with numeric values where possible
    stats: Dict[str, Any] = {}
    for s in entry.get("stats", []):
        k = s.get("name")
        v = s.get("value")
        if v in (None, "-", ""):
            v = s.get("displayValue")
        if k and v not in (None, "-", ""):
            try:
                stats[k] = float(v)
            except Exception:
                stats[k] = v  # keep as string if non-numeric

    # Games played: direct, else wins+losses+ties
    g = _first(stats, "gamesPlayed", default=None)
    if g is None:
        g = _to_int(stats.get("wins", 0)) + _to_int(stats.get("losses", 0)) + _to_int(stats.get("ties", 0))

    # PF/PA across sports (handle camelCase / lowercase variants)
    pf = _first(stats, "pointsFor", "pointsfor", "runsFor", "runsfor", default=None)
    pa = _first(stats, "pointsAgainst", "pointsagainst", "runsAgainst", "runsagainst", default=None)

    # IMPORTANT: For NCAAF, PF/PA may be missing in standings -> default to 0 so rows still exist
    pf = _to_int(pf, default=0)
    pa = _to_int(pa, default=0)
    g = _to_int(g, default=0)

    out_list.append([name, g, pf, pa])
    logging.info(str([name, g, pf, pa]))

def fetch_espn_standings(league: str) -> List[List[Any]]:
    logging.info(f"Fetching ESPN standings JSON for {league}...")
    url = ESPN_URLS[league]
    s = _http()
    r = s.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    teams: List[List[Any]] = []
    seen: Set[str] = set()

    # Primary path: children[*].standings.entries[*]
    for child in data.get("children", []) or []:
        standings = (child.get("standings") or {})
        for entry in standings.get("entries", []) or []:
            _extract(entry, teams, seen)

    # Fallback: top-level standings.entries[*]
    if not teams:
        standings = (data.get("standings") or {})
        for entry in standings.get("entries", []) or []:
            _extract(entry, teams, seen)

    logging.info(f"✅ Retrieved data for {len(teams)} {league} teams.")
    return teams

# -----------------------------------------------------------------------------
# Google Sheets IO
# -----------------------------------------------------------------------------
def _open_worksheet(sh, title: str):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="1000", cols="10")

def update_google_sheet(league: str, rows: Iterable[Iterable[Any]]):
    logging.info(f"Updating Google Sheet for {league}...")
    creds = _load_credentials()
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)

    ws = _open_worksheet(sh, league)
    ws.update(HEADER_RANGE, [["Team", "G", "PF", "PA"]])
    ws.batch_clear([BODY_RANGE])

    rows = list(rows)
    if rows:
        ws.update("A2", rows)

    # Timestamp in America/Chicago (CDT/CST)
    try:
        now_ct = datetime.now(pytz.timezone("America/Chicago")).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        now_ct = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ws.update("F1", [[f"Last updated: {now_ct}"]])
    logging.info("✅ Sheet updated.")

# -----------------------------------------------------------------------------
# Runner
# -----------------------------------------------------------------------------
def run_scraper():
    """Pull ESPN standings for each league and write to the corresponding tab."""
    summary: Dict[str, int] = {}
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
