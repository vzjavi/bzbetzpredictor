import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytz
import requests
from flask import has_request_context, request
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_SHEET_ID = "1ub_a9jetvc9BB6paGVIQ_0N_ETXLMEG43tD7zeE3Ljg"
TRACKING_SHEET = os.environ.get("PREDICTIONS_SHEET_NAME", "Predictions")
LOCAL_TIMEZONE = pytz.timezone("America/Chicago")
MODEL_VERSION = os.environ.get("BZ_MODEL_VERSION", "baseline_pfpa_v1")

HEADERS = [
    "snapshot_id", "game_date", "league", "game_time_ct", "away_team", "home_team",
    "away_team_raw", "home_team_raw", "market_total", "bz_total", "edge", "edge_tier",
    "pick", "bookmaker_count", "odds_event_id", "snapshot_at_ct", "actual_away",
    "actual_home", "actual_total", "result", "graded_at_ct", "status", "model_version",
    "last_market_total", "last_market_at_ct", "clv", "clv_status", "canonical_key",
    "snapshot_source",
]

ESPN_SCOREBOARD_URLS = {
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "NCAAF": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
}
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BZBets/2.3; +https://example.com)",
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

_last_grade_attempt: Optional[datetime] = None


def _load_credentials() -> Credentials:
    blob = os.environ.get("GOOGLE_CREDS_JSON")
    if blob:
        return Credentials.from_service_account_info(json.loads(blob), scopes=SCOPES)

    for path in [
        "/etc/secrets/service_account.json",
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        os.path.join(os.path.dirname(__file__), "service_account.json"),
    ]:
        if path and os.path.exists(path):
            return Credentials.from_service_account_file(path, scopes=SCOPES)
    raise RuntimeError("No Google credentials found for BZ Bets prediction tracking.")


def _sheets_service():
    return build("sheets", "v4", credentials=_load_credentials(), cache_discovery=False)


def _sheet_id() -> str:
    return os.environ.get("GOOGLE_SHEETS_ID", DEFAULT_SHEET_ID)


def _ensure_tracking_sheet(service) -> None:
    meta = service.spreadsheets().get(
        spreadsheetId=_sheet_id(), fields="sheets.properties.title"
    ).execute()
    titles = {(s.get("properties") or {}).get("title") for s in meta.get("sheets") or []}
    if TRACKING_SHEET not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=_sheet_id(),
            body={"requests": [{"addSheet": {"properties": {"title": TRACKING_SHEET}}}]},
        ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=_sheet_id(),
        range=f"'{TRACKING_SHEET}'!A1:AC1",
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()


def _normalize_team(name: Optional[str]) -> str:
    if not name:
        return ""
    text = name.lower().strip().replace("’", "'").replace("ʻ", "'").replace("`", "'")
    text = re.sub(r"\buniversity of\b", "", text)
    text = re.sub(r"\buniversity\b", "", text)
    text = re.sub(r"\bst\.\b", "state", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return TEAM_NAME_ALIASES.get(text, text)


def _teams_match(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = _normalize_team(a), _normalize_team(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return len(na) >= 5 and len(nb) >= 5 and (na.endswith(nb) or nb.endswith(na))


def _canonical_game_key(
    league: str,
    game_date: str,
    away_team: Optional[str],
    home_team: Optional[str],
) -> str:
    return (
        f"{league or 'UNKNOWN'}:{game_date or 'unknown'}:"
        f"{_normalize_team(away_team)}:{_normalize_team(home_team)}"
    )


def _prediction_game_date(prediction: Dict[str, Any]) -> str:
    game_time = prediction.get("game_time")
    if isinstance(game_time, datetime):
        try:
            return game_time.astimezone(LOCAL_TIMEZONE).date().isoformat()
        except Exception:
            return game_time.date().isoformat()
    return datetime.now(LOCAL_TIMEZONE).date().isoformat()


def _snapshot_id(prediction: Dict[str, Any]) -> str:
    return _canonical_game_key(
        prediction.get("sport") or "UNKNOWN",
        _prediction_game_date(prediction),
        prediction.get("away_team_raw") or prediction.get("team1"),
        prediction.get("home_team_raw") or prediction.get("team2"),
    )


def _row_canonical_key(row: Dict[str, Any]) -> str:
    return row.get("canonical_key") or _canonical_game_key(
        row.get("league") or "UNKNOWN",
        row.get("game_date") or "unknown",
        row.get("away_team_raw") or row.get("away_team"),
        row.get("home_team_raw") or row.get("home_team"),
    )


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return LOCAL_TIMEZONE.localize(dt)
        return dt.astimezone(LOCAL_TIMEZONE)
    except Exception:
        return None


def _prediction_game_time_local(prediction: Dict[str, Any]) -> Optional[datetime]:
    game_time = prediction.get("game_time")
    if not isinstance(game_time, datetime):
        return None
    try:
        if game_time.tzinfo is None:
            return LOCAL_TIMEZONE.localize(game_time)
        return game_time.astimezone(LOCAL_TIMEZONE)
    except Exception:
        return None


def _read_tracking_rows(service=None, dedupe: bool = True) -> List[Dict[str, Any]]:
    service = service or _sheets_service()
    _ensure_tracking_sheet(service)
    values = service.spreadsheets().values().get(
        spreadsheetId=_sheet_id(), range=f"'{TRACKING_SHEET}'!A2:AC"
    ).execute().get("values", [])

    rows = []
    for sheet_row, values_row in enumerate(values, start=2):
        padded = list(values_row) + [""] * (len(HEADERS) - len(values_row))
        row = dict(zip(HEADERS, padded[:len(HEADERS)]))
        row["_sheet_row"] = sheet_row
        row["_canonical_key"] = _row_canonical_key(row)
        rows.append(row)

    if not dedupe:
        return rows

    canonical: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = row["_canonical_key"]
        current = canonical.get(key)
        if current is None:
            canonical[key] = row
            continue
        row_dt = _parse_dt(row.get("snapshot_at_ct"))
        cur_dt = _parse_dt(current.get("snapshot_at_ct"))
        if row_dt and cur_dt and row_dt < cur_dt:
            canonical[key] = row
        elif (not row_dt or not cur_dt) and row["_sheet_row"] < current["_sheet_row"]:
            canonical[key] = row
    return list(canonical.values())


def _source_name() -> str:
    if has_request_context():
        if request.path.startswith("/admin/tracking"):
            return "scheduled_or_admin"
        if request.path.startswith("/admin/"):
            return "admin"
        return "web"
    return "background"


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clv_value(pick: str, first_line: float, last_line: float) -> Optional[float]:
    pick = (pick or "").upper()
    if pick == "OVER":
        return round(last_line - first_line, 2)
    if pick == "UNDER":
        return round(first_line - last_line, 2)
    return None


def _freeze_prediction_from_row(
    prediction: Dict[str, Any],
    row: Dict[str, Any],
) -> Dict[str, Any]:
    """Replace live/recomputed fields with the immutable first pregame snapshot."""
    bz_total = _to_float(row.get("bz_total"))
    market_total = _to_float(row.get("market_total"))
    edge = _to_float(row.get("edge"))

    if bz_total is not None:
        prediction["predicted_total"] = bz_total
    prediction["market_total"] = market_total
    prediction["edge"] = edge
    # Started games must never re-enter Best Plays.
    prediction["abs_edge"] = None
    prediction["pick"] = row.get("pick") or "NO BET"
    prediction["edge_tier"] = row.get("edge_tier") or ""
    prediction["bookmaker_count"] = _to_int(row.get("bookmaker_count"))
    prediction["odds_event_id"] = row.get("odds_event_id") or prediction.get("odds_event_id")
    prediction["model_version"] = row.get("model_version") or prediction.get("model_version")
    prediction["best_book"] = None
    prediction["best_book_key"] = None
    prediction["best_line"] = None
    prediction["best_price"] = None
    prediction["best_edge"] = None
    prediction["best_abs_edge"] = None
    prediction["line_improvement"] = None
    prediction["game_started"] = True
    prediction["pick_locked"] = True
    prediction["tracking_status"] = "PREGAME_LOCKED"
    return prediction


def _mark_missed_pregame(prediction: Dict[str, Any]) -> Dict[str, Any]:
    """Never turn a live market into a new tracked pregame wager."""
    prediction["market_total"] = None
    prediction["edge"] = None
    prediction["abs_edge"] = None
    prediction["pick"] = "NO BET"
    prediction["edge_tier"] = "Missed pregame"
    prediction["bookmaker_count"] = 0
    prediction["best_book"] = None
    prediction["best_book_key"] = None
    prediction["best_line"] = None
    prediction["best_price"] = None
    prediction["best_edge"] = None
    prediction["best_abs_edge"] = None
    prediction["line_improvement"] = None
    prediction["game_started"] = True
    prediction["pick_locked"] = False
    prediction["tracking_status"] = "MISSED_PREGAME"
    return prediction


def record_prediction_snapshots(predictions: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    prediction_list = list(predictions)
    eligible = [p for p in prediction_list if p.get("market_total") is not None]
    if not eligible:
        return {
            "eligible": 0,
            "inserted": 0,
            "existing": 0,
            "line_updates": 0,
            "locked": 0,
            "missed_pregame": 0,
        }

    try:
        service = _sheets_service()
        raw_rows = _read_tracking_rows(service, dedupe=False)
        canonical_rows = _read_tracking_rows(service, dedupe=True)
        by_key = {row["_canonical_key"]: row for row in canonical_rows}
        now_local = datetime.now(LOCAL_TIMEZONE)
        source = _source_name()
        append_rows, metadata_updates = [], []
        line_updates = 0
        locked = 0
        missed_pregame = 0
        existing_count = 0

        for prediction in eligible:
            game_date = _prediction_game_date(prediction)
            key = _canonical_game_key(
                prediction.get("sport") or "UNKNOWN",
                game_date,
                prediction.get("away_team_raw") or prediction.get("team1"),
                prediction.get("home_team_raw") or prediction.get("team2"),
            )
            current = by_key.get(key)
            game_time_local = _prediction_game_time_local(prediction)
            started = bool(game_time_local and now_local >= game_time_local)
            game_time_ct = game_time_local.isoformat() if game_time_local else ""

            if current is not None:
                existing_count += 1
                if started:
                    _freeze_prediction_from_row(prediction, current)
                    locked += 1
                    continue

                current_market = float(prediction["market_total"])
                model_version = current.get("model_version") or MODEL_VERSION
                snapshot_source = current.get("snapshot_source") or source
                first_line = _to_float(current.get("market_total"))
                last_line = current_market
                last_at = now_local.isoformat()
                clv = (
                    _clv_value(current.get("pick") or "", first_line, last_line)
                    if first_line is not None
                    else None
                )
                line_updates += 1

                if current.get("_sheet_row"):
                    metadata_updates.append({
                        "range": f"'{TRACKING_SHEET}'!W{current['_sheet_row']}:AC{current['_sheet_row']}",
                        "values": [[
                            model_version,
                            last_line,
                            last_at,
                            clv if clv is not None else "",
                            "LAST_OBSERVED_PREGAME",
                            key,
                            snapshot_source,
                        ]],
                    })
                continue

            # No historical snapshot exists. Once the scheduled start time has
            # arrived we refuse to create one from a live/in-game total.
            if started or game_time_local is None:
                _mark_missed_pregame(prediction)
                missed_pregame += 1
                continue

            current_market = float(prediction["market_total"])
            pick = prediction.get("pick") or ""
            clv = _clv_value(pick, current_market, current_market)
            model_version = prediction.get("model_version") or MODEL_VERSION
            row = [
                key,
                game_date,
                prediction.get("sport") or "",
                game_time_ct,
                prediction.get("team1") or "",
                prediction.get("team2") or "",
                prediction.get("away_team_raw") or prediction.get("team1") or "",
                prediction.get("home_team_raw") or prediction.get("team2") or "",
                current_market,
                prediction.get("predicted_total"),
                prediction.get("edge"),
                prediction.get("edge_tier") or "",
                pick,
                prediction.get("bookmaker_count") or 0,
                prediction.get("odds_event_id") or "",
                now_local.isoformat(),
                "",
                "",
                "",
                "",
                "",
                "PENDING",
                model_version,
                current_market,
                now_local.isoformat(),
                clv if clv is not None else "",
                "FIRST_OBSERVED",
                key,
                source,
            ]
            append_rows.append(row)
            by_key[key] = {
                **dict(zip(HEADERS, row)),
                "_sheet_row": None,
                "_canonical_key": key,
            }

        if append_rows:
            service.spreadsheets().values().append(
                spreadsheetId=_sheet_id(),
                range=f"'{TRACKING_SHEET}'!A:AC",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": append_rows},
            ).execute()

        if metadata_updates:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=_sheet_id(),
                body={"valueInputOption": "RAW", "data": metadata_updates},
            ).execute()

        return {
            "eligible": len(eligible),
            "inserted": len(append_rows),
            "existing": existing_count,
            "line_updates": line_updates,
            "locked": locked,
            "missed_pregame": missed_pregame,
            "duplicates_ignored": max(0, len(raw_rows) - len(canonical_rows)),
        }
    except Exception as exc:
        logging.exception("Prediction snapshot tracking failed: %s", exc)
        return {
            "eligible": len(eligible),
            "inserted": 0,
            "existing": 0,
            "line_updates": 0,
            "locked": 0,
            "missed_pregame": 0,
            "error": 1,
        }


def _score_from_competitor(competitor: Dict[str, Any]) -> Optional[float]:
    raw = competitor.get("score")
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("displayValue")
    return _to_float(raw)


def _fetch_completed_mlb_games(game_date: str) -> Optional[List[Dict[str, Any]]]:
    """Use MLB's own schedule API for MLB grading.

    Returns None only when the provider request fails, allowing ESPN fallback.
    """
    try:
        response = requests.get(
            MLB_SCHEDULE_URL,
            params={"sportId": 1, "date": game_date},
            headers=HTTP_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json() or {}
    except Exception as exc:
        logging.warning("MLB StatsAPI grading fetch failed for %s: %s", game_date, exc)
        return None

    completed: List[Dict[str, Any]] = []
    for date_bucket in data.get("dates") or []:
        for game in date_bucket.get("games") or []:
            status = game.get("status") or {}
            if not (
                status.get("abstractGameState") == "Final"
                or status.get("detailedState") in {"Final", "Game Over", "Completed Early"}
                or status.get("codedGameState") == "F"
            ):
                continue

            teams = game.get("teams") or {}
            away = teams.get("away") or {}
            home = teams.get("home") or {}
            away_name = (away.get("team") or {}).get("name")
            home_name = (home.get("team") or {}).get("name")
            away_score = _to_float(away.get("score"))
            home_score = _to_float(home.get("score"))
            if (
                not away_name
                or not home_name
                or away_score is None
                or home_score is None
            ):
                continue

            completed.append({
                "home_team": home_name,
                "away_team": away_name,
                "home_score": home_score,
                "away_score": away_score,
                "actual_total": home_score + away_score,
            })
    return completed


def _fetch_completed_espn_games(league: str, game_date: str) -> List[Dict[str, Any]]:
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
        logging.warning(
            "ESPN scoreboard grading fetch failed for %s %s: %s",
            league,
            game_date,
            exc,
        )
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
        home_name = (
            (home.get("team") or {}).get("displayName")
            or (home.get("team") or {}).get("name")
        )
        away_name = (
            (away.get("team") or {}).get("displayName")
            or (away.get("team") or {}).get("name")
        )
        home_score = _score_from_competitor(home)
        away_score = _score_from_competitor(away)
        if (
            not home_name
            or not away_name
            or home_score is None
            or away_score is None
        ):
            continue
        completed.append({
            "home_team": home_name,
            "away_team": away_name,
            "home_score": home_score,
            "away_score": away_score,
            "actual_total": home_score + away_score,
        })
    return completed


def _fetch_completed_games(league: str, game_date: str) -> List[Dict[str, Any]]:
    if league == "MLB":
        mlb_games = _fetch_completed_mlb_games(game_date)
        if mlb_games is not None:
            return mlb_games
    return _fetch_completed_espn_games(league, game_date)


def _find_completed_game(
    row: Dict[str, Any],
    games: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    row_home = row.get("home_team_raw") or row.get("home_team")
    row_away = row.get("away_team_raw") or row.get("away_team")
    for game in games:
        if _teams_match(row_home, game.get("home_team")) and _teams_match(
            row_away, game.get("away_team")
        ):
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
    global _last_grade_attempt
    now = datetime.now(LOCAL_TIMEZONE)
    if (
        not force
        and _last_grade_attempt
        and now - _last_grade_attempt < timedelta(minutes=10)
    ):
        return {"checked": 0, "graded": 0, "throttled": 1}
    _last_grade_attempt = now

    try:
        service = _sheets_service()
        rows = _read_tracking_rows(service, dedupe=True)
        cutoff = now.date() - timedelta(days=max_days)
        pending = []
        for row in rows:
            if (row.get("status") or "").upper() == "FINAL":
                continue
            try:
                game_date = datetime.strptime(
                    row.get("game_date") or "", "%Y-%m-%d"
                ).date()
            except ValueError:
                continue
            if cutoff <= game_date <= now.date():
                pending.append(row)

        scoreboard_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        updates = []
        matched_games = 0
        for row in pending:
            key = (row.get("league") or "", row.get("game_date") or "")
            if key not in scoreboard_cache:
                scoreboard_cache[key] = _fetch_completed_games(*key)
            game = _find_completed_game(row, scoreboard_cache[key])
            market_total = _to_float(row.get("market_total"))
            if not game or market_total is None:
                continue

            matched_games += 1
            actual_total = float(game["actual_total"])
            updates.append({
                "range": f"'{TRACKING_SHEET}'!Q{row['_sheet_row']}:V{row['_sheet_row']}",
                "values": [[
                    game["away_score"],
                    game["home_score"],
                    actual_total,
                    _grade_pick(row.get("pick") or "", market_total, actual_total),
                    now.isoformat(),
                    "FINAL",
                ]],
            })

        if updates:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=_sheet_id(),
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()

        return {
            "checked": len(pending),
            "graded": len(updates),
            "matched_games": matched_games,
        }
    except Exception as exc:
        logging.exception("Prediction grading failed: %s", exc)
        return {"checked": 0, "graded": 0, "error": 1}


def _aggregate(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    wins = sum(1 for row in rows if row.get("result") == "WIN")
    losses = sum(1 for row in rows if row.get("result") == "LOSS")
    pushes = sum(1 for row in rows if row.get("result") == "PUSH")
    decisions = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "decisions": decisions,
        "record": f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
        "hit_rate": round((wins / decisions) * 100.0, 1) if decisions else 0.0,
        "flat_units": round(wins * (100.0 / 110.0) - losses, 2),
    }


def _filter_state() -> Dict[str, str]:
    state = {"window": "all", "league": "ALL", "pick": "ALL", "tier": "ALL"}
    if not has_request_context():
        return state
    window = (request.args.get("window") or "all").lower()
    league = (request.args.get("league") or "ALL").upper()
    pick = (request.args.get("pick") or "ALL").upper()
    tier = request.args.get("tier") or "ALL"
    if window in {"7", "30", "season", "all"}:
        state["window"] = window
    if league in {"ALL", "MLB", "NFL", "NCAAF", "NBA"}:
        state["league"] = league
    if pick in {"ALL", "OVER", "UNDER"}:
        state["pick"] = pick
    if tier in {"ALL", "Strong Edge", "Good Edge", "Lean"}:
        state["tier"] = tier
    return state


def _row_in_filter(row: Dict[str, Any], filters: Dict[str, str]) -> bool:
    if filters["league"] != "ALL" and row.get("league") != filters["league"]:
        return False
    if filters["pick"] != "ALL" and row.get("pick") != filters["pick"]:
        return False
    if filters["tier"] != "ALL" and row.get("edge_tier") != filters["tier"]:
        return False
    try:
        game_date = datetime.strptime(
            row.get("game_date") or "", "%Y-%m-%d"
        ).date()
    except ValueError:
        return False
    today, window = datetime.now(LOCAL_TIMEZONE).date(), filters["window"]
    if window == "7" and game_date < today - timedelta(days=6):
        return False
    if window == "30" and game_date < today - timedelta(days=29):
        return False
    if window == "season" and game_date.year != today.year:
        return False
    return True


def _public_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "game_date": row.get("game_date") or "",
        "league": row.get("league") or "",
        "away_team": row.get("away_team") or "",
        "home_team": row.get("home_team") or "",
        "market_total": _to_float(row.get("market_total")),
        "last_market_total": _to_float(row.get("last_market_total")),
        "bz_total": _to_float(row.get("bz_total")),
        "edge": _to_float(row.get("edge")),
        "edge_tier": row.get("edge_tier") or "",
        "pick": row.get("pick") or "",
        "actual_total": _to_float(row.get("actual_total")),
        "result": row.get("result") or "",
        "status": row.get("status") or "",
        "clv": _to_float(row.get("clv")),
        "model_version": row.get("model_version") or "legacy_baseline",
    }


def _clv_summary(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    values = [
        v
        for row in rows
        if row.get("pick") in {"OVER", "UNDER"}
        for v in [_to_float(row.get("clv"))]
        if v is not None
    ]
    if not values:
        return {"count": 0, "average": 0.0, "positive_rate": 0.0}
    return {
        "count": len(values),
        "average": round(sum(values) / len(values), 2),
        "positive_rate": round(
            sum(1 for v in values if v > 0) / len(values) * 100.0, 1
        ),
    }


def get_performance_dashboard() -> Dict[str, Any]:
    try:
        service = _sheets_service()
        raw_rows = _read_tracking_rows(service, dedupe=False)
        canonical_rows = _read_tracking_rows(service, dedupe=True)
    except Exception as exc:
        logging.exception("Performance dashboard read failed: %s", exc)
        return {
            "overall": _aggregate([]),
            "by_league": [],
            "by_tier": [],
            "by_pick": [],
            "by_model_version": [],
            "recent": [],
            "tracked_games": 0,
            "total_tracked_games": 0,
            "pending_bets": 0,
            "graded_bets": 0,
            "pass_games": 0,
            "duplicates_ignored": 0,
            "clv": _clv_summary([]),
            "filters": _filter_state(),
            "error": str(exc),
        }

    filters = _filter_state()
    rows = [row for row in canonical_rows if _row_in_filter(row, filters)]
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
        if row.get("pick") in {"OVER", "UNDER"}
        and row.get("status") != "FINAL"
    )

    def breakdown(field: str, labels: List[str]):
        out = []
        for label in labels:
            subset = [
                row
                for row in graded_bets
                if (row.get(field) or "legacy_baseline") == label
            ]
            if subset:
                out.append({"label": label, **_aggregate(subset)})
        return out

    versions = sorted({
        row.get("model_version") or "legacy_baseline"
        for row in graded_bets
    })
    recent = sorted(
        (
            _public_row(row)
            for row in rows
            if row.get("status") == "FINAL"
        ),
        key=lambda row: row.get("game_date") or "",
        reverse=True,
    )[:50]

    return {
        "overall": _aggregate(graded_bets),
        "by_league": breakdown("league", ["MLB", "NFL", "NCAAF", "NBA"]),
        "by_tier": breakdown("edge_tier", ["Strong Edge", "Good Edge", "Lean"]),
        "by_pick": breakdown("pick", ["OVER", "UNDER"]),
        "by_model_version": breakdown("model_version", versions),
        "recent": recent,
        "tracked_games": len(rows),
        "total_tracked_games": len(canonical_rows),
        "graded_bets": len(graded_bets),
        "pending_bets": pending_bets,
        "pass_games": sum(1 for row in rows if row.get("pick") == "PASS"),
        "duplicates_ignored": max(0, len(raw_rows) - len(canonical_rows)),
        "clv": _clv_summary(rows),
        "filters": filters,
        "model_version": MODEL_VERSION,
    }
