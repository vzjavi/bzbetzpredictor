import os
import json
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

import requests
from requests.adapters import HTTPAdapter, Retry
import gspread
from google.oauth2.service_account import Credentials
import pytz
import re

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
    blob = os.environ.get("GOOGLE_CREDS_JSON")
    if blob:
        return Credentials.from_service_account_info(json.loads(blob), scopes=SCOPES)
    secret = "/etc/secrets/service_account.json"
    if os.path.exists(secret):
        return Credentials.from_service_account_file(secret, scopes=SCOPES)
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac and os.path.exists(gac):
        return Credentials.from_service_account_file(gac, scopes=SCOPES)
    local = os.path.join(os.path.dirname(__file__), "service_account.json")
    if os.path.exists(local):
        return Credentials.from_service_account_file(local, scopes=SCOPES)
    raise RuntimeError("No Google credentials found for espn_scraper.")

# -----------------------------------------------------------------------------
# HTTP (retrying)
# -----------------------------------------------------------------------------
def _http() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "bzbetz-predictor/1.4"})
    retries = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
REC_RX = re.compile(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?\s*$")

def _to_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default

def _num(x: Any) -> float:
    if x in (None, "", "-", "—"):
        return 0.0
    try:
        return float(x)
    except Exception:
        return 0.0

def _parse_record_str(s: str) -> Tuple[int,int,int]:
    if not s:
        return 0,0,0
    m = REC_RX.match(s)
    if not m:
        return 0,0,0
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)

def _looks(name: str, abbr: str, *cands: str) -> bool:
    n = (name or "").lower()
    a = (abbr or "").upper()
    if n in cands: return True
    return a in {c.upper() for c in cands}

# -----------------------------------------------------------------------------
# PF/PA + G extraction (very defensive)
# -----------------------------------------------------------------------------
def _extract_from_stats(stats: List[Dict[str,Any]]) -> Tuple[int,int,int,int,int,int,int]:
    """
    Returns (pf, pa, gp_direct, wins, losses, ties, gp_from_recordish_display)
    - gp_direct: from GP/gamesPlayed style fields
    - gp_from_recordish_display: parsed from any displayValue like '10-3'
    """
    pf = pa = gp_direct = wins = losses = ties = gp_rec_disp = 0

    for s in stats or []:
        name = s.get("name") or ""
        abbr = s.get("abbreviation") or ""
        raw_val = s.get("value")
        if raw_val in (None, "", "-", "—"):
            raw_val = s.get("displayValue")

        # PF/PA
        low = name.lower()
        if name in ("pointsFor", "runsFor") or _looks(low, abbr, "pf", "points_for", "overallpointsfor") or ("points" in low and ("for" in low or "scored" in low)) or abbr.upper()=="PF":
            pf = max(pf, _to_int(_num(raw_val), 0))
        if name in ("pointsAgainst", "runsAgainst") or _looks(low, abbr, "pa", "points_against", "overallpointsagainst") or ("points" in low and ("against" in low or "allowed" in low)) or abbr.upper()=="PA":
            pa = max(pa, _to_int(_num(raw_val), 0))

        # GP direct
        if _looks(low, abbr, "gamesplayed", "games", "gp") or ("overall" in low and ("gamesplayed" in low or low.endswith("gp"))):
            gp_direct = max(gp_direct, _to_int(_num(raw_val), 0))

        # Wins/Losses/Ties (various)
        if _looks(low, abbr, "wins", "w", "overallwins"):
            wins = max(wins, _to_int(_num(raw_val), 0))
        if _looks(low, abbr, "losses", "l", "overalllosses"):
            losses = max(losses, _to_int(_num(raw_val), 0))
        if _looks(low, abbr, "ties", "t", "overallties"):
            ties = max(ties, _to_int(_num(raw_val), 0))

        # Sometimes a generic "record" appears as a displayValue like "7-3"
        disp = s.get("displayValue")
        if isinstance(disp, str):
            w,l,t = _parse_record_str(disp)
            gp_rec_disp = max(gp_rec_disp, w + l + t)

    return pf, pa, gp_direct, wins, losses, ties, gp_rec_disp

def _extract_from_records_block(block: Dict[str,Any]) -> Tuple[int,int,int,int]:
    """
    From a single record object -> (gp, w, l, t)
    Considers 'summary' and nested 'stats' names/abbreviations.
    """
    gp = w = l = t = 0

    # "summary": "10-2" etc.
    w2,l2,t2 = _parse_record_str(block.get("summary") or "")
    w = max(w, w2); l = max(l, l2); t = max(t, t2)

    for s in block.get("stats", []) or []:
        name = (s.get("name") or "").lower()
        abbr = (s.get("abbreviation") or "")
        raw_val = s.get("value")
        if raw_val in (None, "", "-", "—"):
            raw_val = s.get("displayValue")
        v = _num(raw_val)

        if _looks(name, abbr, "wins", "w"):
            w = max(w, _to_int(v, 0))
        elif _looks(name, abbr, "losses", "l"):
            l = max(l, _to_int(v, 0))
        elif _looks(name, abbr, "ties", "t"):
            t = max(t, _to_int(v, 0))
        elif _looks(name, abbr, "gamesplayed", "games", "gp"):
            gp = max(gp, _to_int(v, 0))

        # Some feeds put record-like strings in displayValue here, too
        disp = s.get("displayValue")
        if isinstance(disp, str):
            w3,l3,t3 = _parse_record_str(disp)
            gp = max(gp, w3 + l3 + t3)
            w = max(w, w3); l = max(l, l3); t = max(t, t3)

    if gp == 0:
        gp = w + l + t
    return gp, w, l, t

def _extract_pf_pa_g(entry: Dict[str,Any]) -> Tuple[int,int,int]:
    """
    Pull PF/PA/G from both 'stats' and 'record(s)' aggressively.
    """
    stats = entry.get("stats", []) or []
    pf, pa, gp_direct, w_s, l_s, t_s, gp_from_disp = _extract_from_stats(stats)

    gp_candidates = [gp_direct, gp_from_disp]

    # singular 'record'
    if isinstance(entry.get("record"), dict):
        gp1, w1, l1, t1 = _extract_from_records_block(entry["record"])
        gp_candidates.append(gp1)
        # use W/L/T if they look better than stats
        w_s = max(w_s, w1); l_s = max(l_s, l1); t_s = max(t_s, t1)

    # plural 'records'
    if isinstance(entry.get("records"), list):
        for rec in entry["records"]:
            gp2, w2, l2, t2 = _extract_from_records_block(rec)
            gp_candidates.append(gp2)
            w_s = max(w_s, w2); l_s = max(l_s, l2); t_s = max(t_s, t2)

    gp = max(gp_candidates) if gp_candidates else 0
    if gp == 0 and (w_s or l_s or t_s):
        gp = w_s + l_s + t_s

    return _to_int(pf, 0), _to_int(pa, 0), _to_int(gp, 0)

def _pick_better_row(existing: List[Any], incoming: List[Any]) -> List[Any]:
    """
    Prefer higher G; otherwise merge in non-zero PF/PA.
    """
    if not existing:
        return incoming
    name, g1, pf1, pa1 = existing
    _,   g2, pf2, pa2 = incoming
    if g2 > g1:
        return [name, g2, pf2, pa2]
    return [name, g1, pf1 or pf2, pa1 or pa2]

# -----------------------------------------------------------------------------
# Fetch + parse ESPN
# -----------------------------------------------------------------------------
def fetch_espn_standings(league: str) -> List[List[Any]]:
    logging.info(f"Fetching ESPN standings JSON for {league}...")
    r = _http().get(ESPN_URLS[league], timeout=20)
    r.raise_for_status()
    data = r.json()

    team_rows: Dict[str, List[Any]] = {}

    def handle_entry(entry: Dict[str, Any]):
        team = (entry.get("team") or {})
        name = team.get("displayName") or team.get("shortDisplayName") or team.get("name")
        if not name:
            return
        pf, pa, g = _extract_pf_pa_g(entry)
        incoming = [name, g, pf, pa]
        prev = team_rows.get(name)
        best = _pick_better_row(prev, incoming) if prev else incoming
        team_rows[name] = best
        logging.info(str(best))

    # Primary path
    for child in data.get("children") or []:
        for entry in (child.get("standings") or {}).get("entries", []) or []:
            handle_entry(entry)

    # Fallback
    if not team_rows:
        for entry in (data.get("standings") or {}).get("entries", []) or []:
            handle_entry(entry)

    rows = list(team_rows.values())
    logging.info(f"✅ Retrieved data for {len(rows)} {league} teams.")
    return rows

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
    summary: Dict[str, int] = {}
    for lg in ["MLB", "NBA", "NFL", "NCAAF"]:
        try:
            rows = fetch_espn_standings(lg)
            summary[lg] = len(rows)
            update_google_sheet(lg, rows)
        except Exception as e:
            logging.exception(f"❌ {lg} scraping failed: {e}")
            summary[lg] = 0
    return summary

if __name__ == "__main__":
    run_scraper()
