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
    s.headers.update({"User-Agent": "bzbetz-predictor/1.2"})
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

def _looks_like_pf(stat_name: str, abbr: str) -> bool:
    n = (stat_name or "").lower()
    a = (abbr or "").upper()
    return (
        a == "PF" or
        "pointsfor" in n or
        (("points" in n or "pts" in n) and ("for" in n or "scored" in n)) or
        n in {"pf", "points_for", "pts_for", "overallpointsfor"}
    )

def _looks_like_pa(stat_name: str, abbr: str) -> bool:
    n = (stat_name or "").lower()
    a = (abbr or "").upper()
    return (
        a == "PA" or
        "pointsagainst" in n or
        (("points" in n or "pts" in n) and ("against" in n or "allowed" in n)) or
        n in {"pa", "points_against", "pts_against", "overallpointsagainst"}
    )

def _parse_record_summary(summary: str) -> Tuple[int, int, int]:
    """
    Parse strings like '10-3', '10-2-1', '0-0' etc. Return (wins, losses, ties).
    """
    if not summary:
        return 0, 0, 0
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?\s*$", summary)
    if not m:
        return 0, 0, 0
    w = int(m.group(1))
    l = int(m.group(2))
    t = int(m.group(3) or 0)
    return w, l, t

def _extract_pf_pa_g_from_stats(stats_list: List[Dict[str, Any]]) -> Tuple[int, int, int, int, int, int]:
    """
    Scan the 'stats' array for PF/PA and games/wins/losses/ties.
    Returns: (pf, pa, g, wins, losses, ties)
    """
    pf = pa = g = 0
    wins = losses = ties = 0

    for s in stats_list or []:
        name = s.get("name") or ""
        abbr = s.get("abbreviation") or ""
        raw_val = s.get("value")
        if raw_val in (None, "", "-", "—"):
            raw_val = s.get("displayValue")
        val = _safe_num(raw_val)

        low = name.lower()

        # games played
        if low == "gamesplayed":
            g = _to_int(val, 0)

        # wins/losses/ties
        if low == "wins":
            wins = _to_int(val, wins)
        elif low == "losses":
            losses = _to_int(val, losses)
        elif low == "ties":
            ties = _to_int(val, ties)

        # PF/PA
        if _looks_like_pf(name, abbr) and pf == 0:
            pf = _to_int(val, 0)
        if _looks_like_pa(name, abbr) and pa == 0:
            pa = _to_int(val, 0)

        # exact baseball variants kept for completeness
        if name in ("pointsFor", "runsFor") and pf == 0:
            pf = _to_int(val, 0)
        if name in ("pointsAgainst", "runsAgainst") and pa == 0:
            pa = _to_int(val, 0)

    return pf, pa, g, wins, losses, ties

def _extract_overall_record_from_records(records_list: List[Dict[str, Any]]) -> Tuple[int, int, int, int]:
    """
    Look inside 'records' for type overall/total and derive (G, W, L, T).
    ESPN often provides either:
      - {"type":"overall","summary":"10-3", "stats":[{"name":"wins","value":10}, ...]}
      - or similar variants.
    """
    g = w = l = t = 0
    for rec in records_list or []:
        rtype = (rec.get("type") or rec.get("name") or "").lower()
        if rtype not in {"overall", "total"}:
            continue

        # Try summary first
        w2, l2, t2 = _parse_record_summary(rec.get("summary") or "")
        w = max(w, w2)
        l = max(l, l2)
        t = max(t, t2)

        # Stats nested
        for s in rec.get("stats", []) or []:
            name = (s.get("name") or "").lower()
            val = _safe_num(s.get("value") if s.get("value") not in (None, "", "-", "—") else s.get("displayValue"))
            if name == "wins":
                w = max(w, _to_int(val, 0))
            elif name == "losses":
                l = max(l, _to_int(val, 0))
            elif name == "ties":
                t = max(t, _to_int(val, 0))
            elif name == "gamesplayed" and g == 0:
                g = _to_int(val, 0)

    if g == 0:
        g = w + l + t
    return g, w, l, t

def _extract_pf_pa_g(entry: Dict[str, Any]) -> Tuple[int, int, int]:
    """
    Robustly extract PF, PA, and G using both 'stats' and 'records'.
    """
    stats_list = entry.get("stats", []) or []
    pf, pa, g, w, l, t = _extract_pf_pa_g_from_stats(stats_list)

    # If games not present in stats, try records
    if g == 0:
        g2, w2, l2, t2 = _extract_overall_record_from_records(entry.get("records", []) or [])
        # prefer the richer source
        g = max(g, g2)
        # If PF/PA are still zero, nothing to do here (records usually don't have PF/PA)

    # If still zero, last resort: sum wins/losses/ties we saw in stats
    if g == 0 and (w or l or t):
        g = w + l + t

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
