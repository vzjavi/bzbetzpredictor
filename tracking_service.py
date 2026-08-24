import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytz
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_SHEET_ID = "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg"
TRACKING_SHEET = os.environ.get("PREDICTIONS_SHEET_NAME", "Predictions")
LOCAL_TIMEZONE = pytz.timezone("America/Chicago")

HEADERS = [
    "snapshot_id",
    "game_date",
    "league",
    "game_time_ct",
    "away_team",
    "home_team",
    "away_team_raw",
    "home_team_raw",
    "market_total",
    "bz_total",
    "edge",
    "edge_tier",
    "pick",
    "bookmaker_count",
    "odds_event_id",
    "snapshot_at_ct",
    "actual_away",
    "actual_home",
    "actual_total",
    "result",
    "graded_at_ct",
    "status",
]

ESPN_SCOREBOARD_URLS = {
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "NCAAF": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
}

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BZBets/2.1; +https://example.com)",
    "Accept": "application/json",
}

TEAM_NAME_ALIASES = {
    "oakland athletics": "athletics",
    "southern california": "usc",
    "louisiana state": "lsu",
    "brigham young": "byu",
    "massachusetts": "umass",
    "mississippi": "ole miss",
    "california": "cal",
}

_existing_ids_cache: Tuple[Optional[datetime], set] = (None, set())
_last_grade_attempt: Optional[datetime] = None


def _load_credentials() -> Credentials:
    blob = os.environ.get("GOOGLE_CREDS_JSON")
    if blob:
        return Credentials.from_service_account_info(json.loads(blob), scopes=SCOPES)

    candidates = [
        "/etc/secrets/service_account.json",
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        os.path.join(os.path.dirname(__file__), "service_account.json"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return Credentials.from_service_account_file(path, scopes=SCOPES)

    raise RuntimeError("No Google credentials found for BZ Bets prediction tracking.")


def _sheets_service():
    creds = _load_credentials()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _sheet_id() -> str:
    return os.environ.get("GOOGLE_SHEETS_ID", DEFAULT_SHEET_ID)


def _ensure_tracking_sheet(service) -> None:
    sheet_id = _sheet_id()
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets.properties.title",
    ).execute()
    titles = {
        (sheet.get("properties") or {}).get("title")
        for sheet in meta.get("sheets") or []
    }

    if TRACKING_SHEET not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": TRACKING_SHEET}}}]},
        ).execute()

    header_range = f"'{TRACKING_SHEET}'!A1:V1"
    current = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=header_range,
    ).execute().get("values", [])
    if not current or current[0] != HEADERS:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=header_range,
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()


def _normalize_team(name: Optional[str]) -> str:
    if not name:
        return ""
    text = name.lower().strip()
    text = text.replace("’", "'").replace("ʻ", "'").replace("`", "'")
    text = re.sub(r"\buniversity of\b", "", text)
    text = re.sub(r"\buniversity\b", "", text)
    text = re.sub(r"\bst\.\b", "state", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return TEAM_NAME_ALIASES.get(text, text)


def _teams_match(a: Optional[str], b: Optional[str]) -> bool:
    na = _normalize_team(a)
    nb = _normalize_team(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 5 and len(nb) >= 5:
        return na.endswith(nb) or nb.endswith(na)
    return False


def _snapshot_id(prediction: Dict[str, Any]) -> str:
    league = prediction.get("sport") or "UNKNOWN"
    event_id = prediction.get("odds_event_id")
    if event_id:
        return f"{league}:{event_id}"

    game_time = prediction.get("game_time")
    game_date = game_time.date().isoformat() if isinstance(game_time, datetime) else "unknown"
    away = _normalize_team(prediction.get("away_team_raw") or prediction.get("team1"))
    home = _normalize_team(prediction.get("home_team_raw") or prediction.get("team2"))
    return f"{league}:{game_date}:{away}:{home}"


def _existing_snapshot_ids(service, force: bool = False) -> set:
    global _existing_ids_cache
    fetched_at, cached_ids = _existing_ids_cache
    now = datetime.utcnow()
    if not force and fetched_at and now - fetched_at < timedelta(minutes=2):
        return set(cached_ids)

    _ensure_tracking_sheet(service)
    values = service.spreadsheets().values().get(
        spreadsheetId=_sheet_id(),
        range=f"'{TRACKING_SHEET}'!A2:A",
    ).execute().get("values", [])
    ids = {row[0] for row in values if row and row[0]}
    _existing_ids_cache = (now, set(ids))
    return ids


def record_prediction_snapshots(predictions: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Append the first market snapshot seen for each game. Existing games are never rewritten."""
    global _existing_ids_cache

    eligible = [p for p in predictions if p.get("market_total") is not None]
    if not eligible:
        return {"eligible": 0, "inserted": 0, "existing": 0}

    try:
        service = _sheets_service()
        existing_ids = _existing_snapshot_ids(service)
        rows = []
        now_local = datetime.now(LOCAL_TIMEZONE)

        for prediction in eligible:
            snapshot_id = _snapshot_id(prediction)
            if snapshot_id in existing_ids:
                continue

            game_time = prediction.get("game_time")
            if isinstance(game_time, datetime):
                game_date = game_time.astimezone(LOCAL_TIMEZONE).date().isoformat()
                game_time_ct = game_time.astimezone(LOCAL_TIMEZONE).isoformat()
            else:
                game_date = now_local.date().isoformat()
                game_time_ct = ""

            rows.append(
                [
                    snapshot_id,
                    game_date,
                    prediction.get("sport") or "",
                    game_time_ct,
                    prediction.get("team1") or "",
                    prediction.get("team2") or "",
                    prediction.get("away_team_raw") or prediction.get("team1") or "",
                    prediction.get("home_team_raw") or prediction.get("team2") or "",
                    prediction.get("market_total"),
                    prediction.get("predicted_total"),
                    prediction.get("edge"),
                    prediction.get("edge_tier") or "",
                    prediction.get("pick") or "",
                    prediction.get("bookmaker_count") or 0,
                    prediction.get("odds_event_id") or "",
                    now_local.isoformat(),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "PENDING",
                ]
            )
            existing_ids.add(snapshot_id)

        if rows:
            service.spreadsheets().values().append(
                spreadsheetId=_sheet_id(),
                range=f"'{TRACKING_SHEET}'!A:V",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()

        _existing_ids_cache = (datetime.utcnow(), set(existing_ids))
        return {
            "eligible": len(eligible),
            "inserted": len(rows),
            "existing": len(eligible) - len(rows),
        }
    except Exception as exc:
        logging.exception("Prediction snapshot tracking failed: %s", exc)
        return {"eligible": len(eligible), "inserted": 0, "existing": 0, "error": 1}


def _read_tracking_rows(service=None) -> List[Dict[str, Any]]:
    service = service or _sheets_service()
    _ensure_tracking_sheet(service)
    values = service.spreadsheets().values().get(
        spreadsheetId=_sheet_id(),
        range=f"'{TRACKING_SHEET}'!A2:V",
    ).execute().get("values", [])

    rows = []
    for sheet_row, values_row in enumerate(values, start=2):
        padded = list(values_row) + [""] * (len(HEADERS) - len(values_row))
        row = dict(zip(HEADERS, padded[: len(HEADERS)]))
        row["_sheet_row"] = sheet_row
        rows.append(row)
    return rows


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_from_competitor(competitor: Dict[str, Any]) -> Optional[float]:
    raw = competitor.get("score")
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("displayValue")
    return _to_float(raw)


def _fetch_completed_games(league: str, game_date: str) -> List[Dict[str, Any]]:
    url = ESPN_SCOREBOARD_URLS.get(league)
    if not url:
        return []

    try:
        ymd = datetime.strptime(game_date, "%Y-%m-%d").strftime("%Y%m%d")
        response = requests.get(
            url,
            params={"dates": ymd, "limit": 1000},
            headers=HTTP_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        events = response.json().get("events") or []
    except Exception as exc:
        logging.warning("ESPN scoreboard grading fetch failed for %s %s: %s", league, game_date, exc)
        return []

    completed = []
    for event in events:
        status_type = ((event.get("status") or {}).get("type") or {})
        if not (status_type.get("completed") or status_type.get("state") == "post"):
            continue

        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competitors = competitions[0].get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        home_name = (home.get("team") or {}).get("displayName") or (home.get("team") or {}).get("name")
        away_name = (away.get("team") or {}).get("displayName") or (away.get("team") or {}).get("name")
        home_score = _score_from_competitor(home)
        away_score = _score_from_competitor(away)
        if not home_name or not away_name or home_score is None or away_score is None:
            continue

        completed.append(
            {
                "home_team": home_name,
                "away_team": away_name,
                "home_score": home_score,
                "away_score": away_score,
                "actual_total": home_score + away_score,
            }
        )
    return completed


def _find_completed_game(row: Dict[str, Any], games: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    row_home = row.get("home_team_raw") or row.get("home_team")
    row_away = row.get("away_team_raw") or row.get("away_team")
    for game in games:
        if _teams_match(row_home, game.get("home_team")) and _teams_match(row_away, game.get("away_team")):
            return game
    return None


def _grade_pick(pick: str, market_total: float, actual_total: float) -> str:
    pick = (pick or "").upper()
    if pick == "PASS":
        return "PASS"
    if actual_total == market_total:
        return "PUSH"
    if pick == "OVER":
        return "WIN" if actual_total > market_total else "LOSS"
    if pick == "UNDER":
        return "WIN" if actual_total < market_total else "LOSS"
    return ""


def grade_ungraded_predictions(max_days: int = 14, force: bool = False) -> Dict[str, int]:
    """Grade pending snapshots from ESPN final scores. Calls are throttled to once per 10 minutes."""
    global _last_grade_attempt

    now = datetime.now(LOCAL_TIMEZONE)
    if not force and _last_grade_attempt and now - _last_grade_attempt < timedelta(minutes=10):
        return {"checked": 0, "graded": 0, "throttled": 1}
    _last_grade_attempt = now

    try:
        service = _sheets_service()
        rows = _read_tracking_rows(service)
        cutoff = now.date() - timedelta(days=max_days)
        pending = []
        for row in rows:
            if (row.get("status") or "").upper() == "FINAL":
                continue
            try:
                game_date = datetime.strptime(row.get("game_date") or "", "%Y-%m-%d").date()
            except ValueError:
                continue
            if cutoff <= game_date <= now.date():
                pending.append(row)

        if not pending:
            return {"checked": 0, "graded": 0}

        scoreboard_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        updates = []

        for row in pending:
            league = row.get("league") or ""
            game_date = row.get("game_date") or ""
            key = (league, game_date)
            if key not in scoreboard_cache:
                scoreboard_cache[key] = _fetch_completed_games(league, game_date)

            game = _find_completed_game(row, scoreboard_cache[key])
            if not game:
                continue

            market_total = _to_float(row.get("market_total"))
            if market_total is None:
                continue
            actual_total = float(game["actual_total"])
            result = _grade_pick(row.get("pick") or "", market_total, actual_total)
            sheet_row = row["_sheet_row"]
            updates.append(
                {
                    "range": f"'{TRACKING_SHEET}'!Q{sheet_row}:V{sheet_row}",
                    "values": [[
                        game["away_score"],
                        game["home_score"],
                        actual_total,
                        result,
                        now.isoformat(),
                        "FINAL",
                    ]],
                }
            )

        if updates:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=_sheet_id(),
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()

        return {"checked": len(pending), "graded": len(updates)}
    except Exception as exc:
        logging.exception("Prediction grading failed: %s", exc)
        return {"checked": 0, "graded": 0, "error": 1}


def _aggregate(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    wins = sum(1 for row in rows if row.get("result") == "WIN")
    losses = sum(1 for row in rows if row.get("result") == "LOSS")
    pushes = sum(1 for row in rows if row.get("result") == "PUSH")
    decisions = wins + losses
    hit_rate = round((wins / decisions) * 100.0, 1) if decisions else 0.0
    flat_units = round(wins * (100.0 / 110.0) - losses, 2)
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "decisions": decisions,
        "record": f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
        "hit_rate": hit_rate,
        "flat_units": flat_units,
    }


def _public_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "game_date": row.get("game_date") or "",
        "league": row.get("league") or "",
        "away_team": row.get("away_team") or "",
        "home_team": row.get("home_team") or "",
        "market_total": _to_float(row.get("market_total")),
        "bz_total": _to_float(row.get("bz_total")),
        "edge": _to_float(row.get("edge")),
        "edge_tier": row.get("edge_tier") or "",
        "pick": row.get("pick") or "",
        "actual_total": _to_float(row.get("actual_total")),
        "result": row.get("result") or "",
        "status": row.get("status") or "",
    }


def get_performance_dashboard() -> Dict[str, Any]:
    try:
        rows = _read_tracking_rows()
    except Exception as exc:
        logging.exception("Performance dashboard read failed: %s", exc)
        return {
            "overall": _aggregate([]),
            "by_league": [],
            "by_tier": [],
            "by_pick": [],
            "recent": [],
            "tracked_games": 0,
            "pending_bets": 0,
            "error": str(exc),
        }

    graded_bets = [
        row
        for row in rows
        if row.get("pick") in {"OVER", "UNDER"}
        and row.get("status") == "FINAL"
        and row.get("result") in {"WIN", "LOSS", "PUSH"}
    ]
    pending_bets = sum(
        1
        for row in rows
        if row.get("pick") in {"OVER", "UNDER"} and row.get("status") != "FINAL"
    )

    by_league = []
    for league in ["MLB", "NFL", "NCAAF", "NBA"]:
        league_rows = [row for row in graded_bets if row.get("league") == league]
        if league_rows:
            by_league.append({"label": league, **_aggregate(league_rows)})

    tier_order = ["Strong Edge", "Good Edge", "Lean"]
    by_tier = []
    for tier in tier_order:
        tier_rows = [row for row in graded_bets if row.get("edge_tier") == tier]
        if tier_rows:
            by_tier.append({"label": tier, **_aggregate(tier_rows)})

    by_pick = []
    for pick in ["OVER", "UNDER"]:
        pick_rows = [row for row in graded_bets if row.get("pick") == pick]
        if pick_rows:
            by_pick.append({"label": pick, **_aggregate(pick_rows)})

    recent = sorted(
        (_public_row(row) for row in rows if row.get("status") == "FINAL"),
        key=lambda row: row.get("game_date") or "",
        reverse=True,
    )[:30]

    return {
        "overall": _aggregate(graded_bets),
        "by_league": by_league,
        "by_tier": by_tier,
        "by_pick": by_pick,
        "recent": recent,
        "tracked_games": len(rows),
        "graded_bets": len(graded_bets),
        "pending_bets": pending_bets,
        "pass_games": sum(1 for row in rows if row.get("pick") == "PASS"),
    }
