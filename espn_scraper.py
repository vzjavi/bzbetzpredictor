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
# HTTP session (retries + UA)
# -----------------------------------------------------------------------------
def _http() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "bzbetz-predictor/1.3"})
    retries = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def _to_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default

def _safe_num(v: Any) -> float:
    if v in (None, "", "-", "—"):
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0

def _parse_record_summary(summary: str) -> Tuple[int, int, int]:
    """Parse '10-3' or '10-2-1' -> (W,L,T)."""
    if not summary:
        return 0, 0, 0
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?\s*$", summary)
    if not m:
        return 0, 0, 0
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)

def _looks(name: str, abbr: str, targets: List[str]) -> bool:
    n = (name or "").lower()
    a = (abbr or "").upper()
    return (n in targets) or (a in [t.upper() for t in targets])

# -----------------------------------------------------------------------------
# PF/PA + G extraction
# -----------------------------------------------------------------------------
def _extract_pf_pa_g_from_stats(stats_list: List[Dict[str, Any]]) -> Tuple[int, int, int, int, int, int]:
    """
    Scan 'stats' for PF/PA and game counters.
    Returns (pf, pa, g, wins, losses, ties).
    """
    pf = pa = g = 0
    wins = losses = ties = 0

    for s in stats_list or []:
        name = s.get("name") or ""
        abbr = s.get("abbreviation") or ""
        raw_val = s.get("value")
        if raw_val in (None, "", "-", "—"):
            raw_val = s.get("displayValue")
        val_num = _safe_num(raw_val)

        lname = name.lower()

        # ---------- games played ----------
        if _looks(lname, abbr, ["gamesplayed", "games", "gp"]):
            g = max(g, _to_int(val_num, 0))
        # Some feeds bury GP as "overallGamesPlayed"
        if "overall" in lname and ("gamesplayed" in lname or lname.endswith("gp")):
            g = max(g, _to_int(val_num, 0))

        # ---------- wins/losses/ties ----------
        if _looks(lname, abbr, ["wins", "w", "overallwins"]):
            wins = max(wins, _to_int(val_num, 0))
        if _looks(lname, abbr, ["losses", "l", "overalllosses"]):
            losses = max(losses, _to_int(val_num, 0))
        if _looks(lname, abbr, ["ties", "t", "overallties"]):
            ties = max(ties, _to_int(val_num, 0))

        # ---------- PF / PA ----------
        # common exact names
        if name in ("pointsFor", "runsFor") and pf == 0:
            pf = _to_int(val_num, 0)
        if name in ("pointsAgainst", "runsAgainst") and pa == 0:
            pa = _to_int(val_num, 0)

        # heuristic matches
        if pf == 0 and (lname in {"pf", "points_for", "overallpointsfor"} or ("points" in lname and ("for" in lname or "scored" in lname)) or abbr.upper() == "PF"):
            pf = _to_int(val_num, 0)
        if pa == 0 and (lname in {"pa", "points_against", "overallpointsagainst"} or ("points" in lname and ("against" in lname or "allowed" in lname)) or abbr.upper() == "PA"):
            pa = _to_int(val_num, 0)

    return pf, pa, g, wins, losses, ties

def _extract_overall_from_records_block(block: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """
    Handle one 'record' or one element of 'records'.
    Return (G,W,L,T) if it looks like an overall/total record.
    """
    g = w = l = t = 0
    rtype = (block.get("type") or block.get("name") or "").lower()
    if rtype not in {"overall", "total", ""}:  # some feeds omit type
        # Not overall; skip but still parse summary if present just in case
        pass

    # summary like "10-3"
    w2, l2, t2 = _parse_record_summary(block.get("summary") or "")
    w = max(w, w2)
    l = max(l, l2)
    t = max(t, t2)

    # nested stats
    for s in block.get("stats", []) or []:
        name = (s.get("name") or "").lower()
        abbr = (s.get("abbreviation") or "")
        raw_val = s.get("value")
        if raw_val in (None, "", "-", "—"):
            raw_val = s.get("displayValue")
        val_num = _safe_num(raw_val)

        if _looks(name, abbr, ["wins", "w"]):
            w = max(w, _to_int(val_num, 0))
        elif _looks(name, abbr, ["losses", "l"]):
            l = max(l, _to_int(val_num, 0))
        elif _looks(name, abbr, ["ties", "t"]):
            t = max(t, _to_int(val_num, 0))
        elif _looks(name, abbr, ["gamesplayed", "games", "gp"]):
            g = max(g, _to_int(val_num, 0))

    if g == 0:
        g = w + l + t
    return g, w, l, t

def _extract_pf_pa_g(entry: Dict[str, Any]) -> Tuple[int, int, int]:
    """
    Combine 'stats' + ('record' or 'records') to get PF, PA, G.
    """
    pf, pa, g_stats, w_stats, l_stats, t_stats = _extract_pf_pa_g_from_stats(entry.get("stats", []) or [])

    g = g_stats
    # Prefer records when stats don't include games
    # singular 'record'
    if g == 0 and isinstance(entry.get("record"), dict):
        g1, w1, l1, t1 = _extract_overall_from_records_block(entry["record"])
        g = max(g, g1)
        # if wins/losses/ties present here but g still 0, use sum
        if g == 0 and (w1 or l1 or t1):
            g = w1 + l1 + t1

    # plural 'records'
    if g == 0 and isinstance(entry.get("records"), list):
        best_g = 0
        for rec in entry["records"]:
            g2, w2, l2, t2 = _extract_overall_from_records_block(rec)
            best_g = max(best_g, g2 or (w2 + l2 + t2))
        g = max(g, best_g)

    # last fallback: wins/losses/ties we found in stats
    if g == 0 and (w_stats or l_stats or t_stats):
        g = w_stats + l_stats + t_stats

    return _to_int(pf, 0), _to_int(pa, 0), _to_int(g, 0)

def _pick_better_row(existing: List[Any], incoming: List[Any]) -> List[Any]:
    """
    Choose the better row between two entries of [name, G, PF, PA]:
      1) Prefer higher G
      2) Merge in non-zero PF/PA if existing are zero
    """
    if not existing:
        return incoming
    name, g1, pf1, pa1 = existing
    _,   g2, pf2, pa2 = incoming
    if g2 > g1:
        return [name, g2, pf2, pa2]
    pf = pf1 if pf1 else pf2
    pa = pa1 if pa1 else pa2
    return [name, g1, pf, pa]

# -----------------------------------------------------------------------------
# ESPN parsing
# -----------------------------------------------------------------------------
def fetch_espn_standings(league: str) -> List[List[Any]]:
    logging.info(f"Fetching ESPN standings JSON for {league}...")
    url = ESPN_URLS[league]
    s = _http()
    r = s.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    # Dict keyed by display name; value is [name, G, PF, PA]
    team_rows: Dict[str, List[Any]] = {}

    def handle_entry(entry: Dict[str, Any]):
        team = (entry.get("team") or {})
        name = team.get("displayName") or team.get("shortDisplayName") or team.get("name")
        if not name:
            return
        pf, pa, g = _extract_pf_pa_g(entry)
        incoming = [name, g, pf, pa]
        existing = team_rows.get(name)
        best = _pick_better_row(existing, incoming) if existing else incoming
        team_rows[name] = best
        logging.info(str(best))

    # Primary path: children[*].standings.entries[*]
    for child in data.get("children") or []:
        for entry in (child.get("standings") or {}).get("entries", []) or []:
            handle_entry(entry)

    # Fallback: top-level standings.entries[*]
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
