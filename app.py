import os
import time
import json
import logging
from datetime import datetime
from difflib import get_close_matches

import pytz
import requests
import pandas as pd
from flask import Flask, render_template, request, abort, jsonify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

# -----------------------------------------------------------------------------
# Flask
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
LOCAL_TZ = pytz.timezone("America/Chicago")

DEFAULT_SHEET_ID = "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg"
SHEET_ID = os.environ.get("GOOGLE_SHEETS_ID", DEFAULT_SHEET_ID)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")  # set on both your Web Service & your Cron Job

API_KEY = "697039"  # TheSportsDB demo key you used
SPORT_LEAGUES = {
    "NBA": "4387",
    "MLB": "4424",
    "NCAAF": "4479",
    "NFL": "4391",
}

# Names that sometimes differ between APIs and your Sheet tabs
ALT_NAMES = {
    "LA Rams": "Los Angeles Rams",
    "LA Chargers": "Los Angeles Chargers",
    "NY Giants": "New York Giants",
    "NY Jets": "New York Jets",
    "SF 49ers": "San Francisco 49ers",
    "KC Chiefs": "Kansas City Chiefs",
    "TB Buccaneers": "Tampa Bay Buccaneers",
    "NO Saints": "New Orleans Saints",
    "WAS Commanders": "Washington Commanders",
}

# tiny cache to reduce Sheets reads (avoid 429 rate limits)
_sheet_cache = {}           # league -> (timestamp, DataFrame)
SHEETS_TTL_SEC = int(os.environ.get("SHEETS_TTL_SEC", "300"))

# -----------------------------------------------------------------------------
# Credentials helper: supports GOOGLE_CREDS_JSON OR /etc/secrets/service_account.json
# -----------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def _load_credentials() -> Credentials:
    # 1) env JSON wins
    json_blob = os.environ.get("GOOGLE_CREDS_JSON")
    if json_blob:
        info = json.loads(json_blob)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    # 2) secret file (Render mounts here)
    secret_path = "/etc/secrets/service_account.json"
    if os.path.exists(secret_path):
        return Credentials.from_service_account_file(secret_path, scopes=SCOPES)

    # 3) GOOGLE_APPLICATION_CREDENTIALS path
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac and os.path.exists(gac):
        return Credentials.from_service_account_file(gac, scopes=SCOPES)

    # 4) local fallback (dev)
    local = os.path.join(os.path.dirname(__file__), "service_account.json")
    if os.path.exists(local):
        return Credentials.from_service_account_file(local, scopes=SCOPES)

    raise RuntimeError("No Google credentials found. Set GOOGLE_CREDS_JSON or add secret file service_account.json")

# -----------------------------------------------------------------------------
# Load logos (optional)
# -----------------------------------------------------------------------------
try:
    with open("team_logos.json", "r", encoding="utf-8") as f:
        LOGOS = json.load(f)
except Exception:
    LOGOS = {}
    logging.warning("team_logos.json missing; logos will be blank.")

# -----------------------------------------------------------------------------
# Admin/Cron endpoints
# -----------------------------------------------------------------------------
def _reset_cache():
    try:
        _sheet_cache.clear()
    except Exception:
        pass

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return ("ok", 200)

@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    if not ADMIN_TOKEN or request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        abort(401)
    _reset_cache()
    return jsonify(ok=True, action="reset")

@app.route("/admin/daily", methods=["POST"])
def admin_daily():
    if not ADMIN_TOKEN or request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        abort(401)
    _reset_cache()
    # Run standings scraper -> updates your Google Sheet tabs
    from espn_scraper import run_scraper
    summary = run_scraper()
    return jsonify(ok=True, summary=summary)

# -----------------------------------------------------------------------------
# Google Sheets read (Team | G | PF | PA)
# -----------------------------------------------------------------------------
def fetch_data_from_sheets(league_tab: str) -> pd.DataFrame:
    now = time.time()
    hit = _sheet_cache.get(league_tab)
    if hit and (now - hit[0]) < SHEETS_TTL_SEC:
        return hit[1]

    creds = _load_credentials()
    service = build("sheets", "v4", credentials=creds)

    rng = f"{league_tab}!A1:D1000"
    res = service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=rng).execute()
    values = res.get("values", [])

    if not values or len(values) < 2:
        df_empty = pd.DataFrame(columns=["G", "PF", "PA"], index=pd.Index([], name="Team"))
        _sheet_cache[league_tab] = (now, df_empty)
        return df_empty

    expected = ["Team", "G", "PF", "PA"]
    rows = []
    for r in values[1:]:
        r = r if isinstance(r, list) else [str(r)]
        r = (r + ["", "", "", ""])[:4]
        rows.append(r)

    df = pd.DataFrame(rows, columns=expected)
    df["Team"] = df["Team"].astype(str).str.strip()
    df = df[df["Team"] != ""]
    for c in ["G", "PF", "PA"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df.set_index("Team", inplace=True)

    _sheet_cache[league_tab] = (now, df)
    return df

# -----------------------------------------------------------------------------
# TheSportsDB helpers
# -----------------------------------------------------------------------------
def get_todays_games(league_name: str):
    league_id = SPORT_LEAGUES[league_name]
    today = datetime.now(LOCAL_TZ).date()

    season_map = {"NBA": "2025-2026", "MLB": "2025", "NFL": "2025", "NCAAF": "2025"}
    season = season_map.get(league_name, "2025")

    url = f"https://www.thesportsdb.com/api/v1/json/{API_KEY}/eventsseason.php?id={league_id}&s={season}"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        events = (resp.json() or {}).get("events") or []

        def is_today(g):
            d, t = g.get("dateEvent"), g.get("strTime")
            if not d or not t:
                return False
            try:
                game_utc = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
                return game_utc.astimezone(LOCAL_TZ).date() == today
            except Exception as e:
                logging.warning(f"Bad game datetime '{d} {t}': {e}")
                return False

        return [g for g in events if is_today(g)]
    except Exception as e:
        logging.error(f"{league_name} schedule fetch failed: {e}")
        return []

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def _find_team(name: str, team_list):
    n = ALT_NAMES.get((name or "").strip(), (name or "").strip())
    if n in team_list:
        return n
    m = get_close_matches(n, team_list, n=1, cutoff=0.6)
    if m:
        return m[0]
    logging.warning(f"⚠️ Team not matched: {name}")
    return None

def _per_game(row):
    try:
        g = float(row.get("G", 0)) or 0.0
        pf = float(row.get("PF", 0)) or 0.0
        pa = float(row.get("PA", 0)) or 0.0
        if g <= 0:
            return 0.0, 0.0
        return pf / g, pa / g
    except Exception:
        return 0.0, 0.0

def _logo(full_name, sport):
    try:
        return (LOGOS.get(sport) or {}).get(full_name)
    except Exception:
        return None

# -----------------------------------------------------------------------------
# Prediction logic
# -----------------------------------------------------------------------------
def predict_game_totals(league_name: str):
    out = []
    games = get_todays_games(league_name)
    logging.info(f"{league_name} games fetched: {len(games)}")

    stats = fetch_data_from_sheets(league_name)
    team_list = stats.index.tolist()
    seen = set()

    for g in games:
        home, away = g.get("strHomeTeam"), g.get("strAwayTeam")

        # time (local)
        when_local = None
        d, t = g.get("dateEvent"), g.get("strTime")
        if d and t:
            try:
                when_local = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc).astimezone(LOCAL_TZ)
            except Exception as e:
                logging.warning(f"Time parse error '{d} {t}': {e}")

        t1 = _find_team(home, team_list)
        t2 = _find_team(away, team_list)
        if not t1 or not t2:
            logging.warning(f"Skip unmatched: {home} vs {away}")
            continue

        key = tuple(sorted([t1, t2]))
        if key in seen:
            continue
        seen.add(key)

        r1, r2 = stats.loc[t1], stats.loc[t2]
        pf1, pa1 = _per_game(r1)
        pf2, pa2 = _per_game(r2)
        total = round((pf1 + pa1 + pf2 + pa2) / 2.0, 1)

        out.append({
            "sport": league_name,
            "team1": t1,
            "team2": t2,
            "team1_logo": _logo(t1, league_name),
            "team2_logo": _logo(t2, league_name),
            "predicted_total": total,
            "game_time": when_local,
        })

    return out

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return ("", 200)

    preds = []
    for sport in ["NBA", "MLB", "NFL", "NCAAF"]:
        p = predict_game_totals(sport)
        logging.info(f"{sport} predictions: {len(p)} games")
        preds.extend(p)

    preds = sorted(preds, key=lambda x: x.get("game_time") or datetime.max)

    # if your Jinja template exists, use it; else render simple HTML
    try:
        return render_template("index.html", predictions=preds, now=datetime.now(LOCAL_TZ))
    except Exception:
        lis = "\n".join(
            f"<li>{x['sport']}: {x['team1']} vs {x['team2']} — {x['predicted_total']}"
            + (f" @ {x['game_time']:%Y-%m-%d %H:%M}" if x.get('game_time') else "")
            + "</li>"
            for x in preds
        )
        return f"<h1>Predictions</h1><ul>{lis}</ul>", 200

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # optional local/dev: set RUN_SCRAPER_ON_START=1 to refresh the sheet on boot
    if os.environ.get("RUN_SCRAPER_ON_START") == "1":
        from espn_scraper import run_scraper
        run_scraper()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
