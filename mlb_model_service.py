import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

MLB_STATS_API = "https://statsapi.mlb.com/api/v1"
MODEL_VERSION = "mlb_form_pitching_v2"
RECENT_GAMES = int(os.environ.get("MLB_RECENT_GAMES", "10"))
RECENT_WEIGHT = float(os.environ.get("MLB_RECENT_WEIGHT", "0.30"))
STARTER_ERA_BASELINE = float(os.environ.get("MLB_STARTER_ERA_BASELINE", "4.20"))
STARTER_ERA_COEFFICIENT = float(os.environ.get("MLB_STARTER_ERA_COEFFICIENT", "0.24"))
STARTER_ADJUSTMENT_CAP = float(os.environ.get("MLB_STARTER_ADJUSTMENT_CAP", "1.25"))
HTTP_TIMEOUT = float(os.environ.get("MLB_STATS_API_TIMEOUT", "12"))

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BZBets/2.3)",
    "Accept": "application/json",
}

TEAM_ALIASES = {
    "oakland athletics": "athletics",
    "athletics": "athletics",
    "la angels": "los angeles angels",
    "los angeles angels of anaheim": "los angeles angels",
}

_cache: Dict[str, Tuple[datetime, Any]] = {}
_CACHE_TTL = timedelta(minutes=15)


def _normalize_team(name: Optional[str]) -> str:
    if not name:
        return ""
    text = str(name).lower().strip()
    text = text.replace("’", "'").replace("ʻ", "'").replace("`", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return TEAM_ALIASES.get(text, text)


def _teams_match(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = _normalize_team(a), _normalize_team(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 5 and len(nb) >= 5:
        return na.endswith(nb) or nb.endswith(na)
    return False


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "-.--"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _cached_json(cache_key: str, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    now = datetime.utcnow()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        response = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        _cache[cache_key] = (now, payload)
        return payload
    except Exception as exc:
        logging.warning("MLB StatsAPI request failed for %s: %s", cache_key, exc)
        return None


def _prediction_date(predictions: Iterable[Dict[str, Any]]) -> date:
    for prediction in predictions:
        game_time = prediction.get("game_time")
        if isinstance(game_time, datetime):
            return game_time.date()
    return date.today()


def _schedule_games_for_date(game_date: date) -> List[Dict[str, Any]]:
    payload = _cached_json(
        f"schedule:{game_date.isoformat()}",
        f"{MLB_STATS_API}/schedule",
        {
            "sportId": 1,
            "date": game_date.isoformat(),
            "hydrate": "team,probablePitcher",
        },
    )
    games = []
    for date_block in (payload or {}).get("dates") or []:
        games.extend(date_block.get("games") or [])
    return games


def _recent_games_payload(game_date: date) -> List[Dict[str, Any]]:
    # A ~24-day window normally contains enough games to build a last-10 sample
    # even with off days, postponements, and the All-Star break.
    start_date = game_date - timedelta(days=24)
    end_date = game_date - timedelta(days=1)
    payload = _cached_json(
        f"recent:{start_date.isoformat()}:{end_date.isoformat()}",
        f"{MLB_STATS_API}/schedule",
        {
            "sportId": 1,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "gameTypes": "R",
        },
    )
    games = []
    for date_block in (payload or {}).get("dates") or []:
        games.extend(date_block.get("games") or [])
    return games


def _final_score(team_blob: Dict[str, Any]) -> Optional[float]:
    return _to_float(team_blob.get("score"))


def _build_recent_form(games: Iterable[Dict[str, Any]], limit: int = RECENT_GAMES) -> Dict[str, Dict[str, Any]]:
    by_team: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for game in games:
        status = ((game.get("status") or {}).get("abstractGameState") or "").lower()
        if status != "final":
            continue

        teams = game.get("teams") or {}
        away, home = teams.get("away") or {}, teams.get("home") or {}
        away_team, home_team = away.get("team") or {}, home.get("team") or {}
        away_name, home_name = away_team.get("name"), home_team.get("name")
        away_score, home_score = _final_score(away), _final_score(home)
        if not away_name or not home_name or away_score is None or home_score is None:
            continue

        game_date = game.get("gameDate") or game.get("officialDate") or ""
        by_team[_normalize_team(away_name)].append(
            {"date": game_date, "runs_for": away_score, "runs_against": home_score}
        )
        by_team[_normalize_team(home_name)].append(
            {"date": game_date, "runs_for": home_score, "runs_against": away_score}
        )

    output: Dict[str, Dict[str, Any]] = {}
    for team_key, team_games in by_team.items():
        team_games.sort(key=lambda row: row.get("date") or "", reverse=True)
        sample = team_games[:limit]
        if not sample:
            continue
        games_count = len(sample)
        output[team_key] = {
            "games": games_count,
            "runs_for_pg": round(sum(g["runs_for"] for g in sample) / games_count, 2),
            "runs_against_pg": round(sum(g["runs_against"] for g in sample) / games_count, 2),
        }
    return output


def _find_recent_form(team_name: Optional[str], recent: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    key = _normalize_team(team_name)
    if key in recent:
        return recent[key]
    for recent_key, data in recent.items():
        if _teams_match(key, recent_key):
            return data
    return None


def _recent_projection(
    away_form: Optional[Dict[str, Any]], home_form: Optional[Dict[str, Any]]
) -> Optional[float]:
    if not away_form or not home_form:
        return None
    if min(int(away_form.get("games") or 0), int(home_form.get("games") or 0)) < 5:
        return None

    away_expected = (
        float(away_form["runs_for_pg"]) + float(home_form["runs_against_pg"])
    ) / 2.0
    home_expected = (
        float(home_form["runs_for_pg"]) + float(away_form["runs_against_pg"])
    ) / 2.0
    return round(away_expected + home_expected, 2)


def _match_schedule_game(
    prediction: Dict[str, Any], schedule_games: Iterable[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    pred_home = prediction.get("home_team_raw") or prediction.get("team2")
    pred_away = prediction.get("away_team_raw") or prediction.get("team1")

    for game in schedule_games:
        teams = game.get("teams") or {}
        home_name = ((teams.get("home") or {}).get("team") or {}).get("name")
        away_name = ((teams.get("away") or {}).get("team") or {}).get("name")
        if _teams_match(pred_home, home_name) and _teams_match(pred_away, away_name):
            return game
    return None


def _probable_pitcher(team_blob: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pitcher = team_blob.get("probablePitcher") or {}
    pitcher_id = pitcher.get("id")
    name = pitcher.get("fullName")
    if not pitcher_id and not name:
        return None
    return {"id": pitcher_id, "name": name or "TBD"}


def _pitcher_stats(person_id: Any, season: int) -> Dict[str, Any]:
    if not person_id:
        return {}
    payload = _cached_json(
        f"pitcher:{person_id}:{season}",
        f"{MLB_STATS_API}/people/{person_id}/stats",
        {"stats": "season", "group": "pitching", "season": season},
    )
    try:
        stat = ((payload or {}).get("stats") or [])[0].get("splits", [])[0].get("stat", {})
    except (IndexError, AttributeError):
        stat = {}
    return {
        "era": _to_float(stat.get("era")),
        "whip": _to_float(stat.get("whip")),
        "innings_pitched": _to_float(stat.get("inningsPitched")),
        "games_started": int(_to_float(stat.get("gamesStarted")) or 0),
    }


def _starter_with_stats(team_blob: Dict[str, Any], season: int) -> Optional[Dict[str, Any]]:
    pitcher = _probable_pitcher(team_blob)
    if not pitcher:
        return None
    pitcher.update(_pitcher_stats(pitcher.get("id"), season))
    return pitcher


def _starter_adjustment(
    away_starter: Optional[Dict[str, Any]], home_starter: Optional[Dict[str, Any]]
) -> float:
    era_deltas = []
    for starter in (away_starter, home_starter):
        era = _to_float((starter or {}).get("era"))
        if era is not None:
            era_deltas.append(era - STARTER_ERA_BASELINE)

    if not era_deltas:
        return 0.0

    adjustment = sum(era_deltas) * STARTER_ERA_COEFFICIENT
    adjustment = max(-STARTER_ADJUSTMENT_CAP, min(STARTER_ADJUSTMENT_CAP, adjustment))
    return round(adjustment, 2)


def _build_notes(
    recent_total: Optional[float], starter_adjustment: float,
    away_starter: Optional[Dict[str, Any]], home_starter: Optional[Dict[str, Any]],
) -> List[str]:
    notes = []
    if recent_total is not None:
        notes.append(f"Last-{RECENT_GAMES} matchup projection: {recent_total:.1f}")
    if away_starter or home_starter:
        starter_bits = []
        for starter in (away_starter, home_starter):
            if not starter:
                continue
            era = starter.get("era")
            if era is None:
                starter_bits.append(starter.get("name") or "TBD")
            else:
                starter_bits.append(f"{starter.get('name') or 'TBD'} {era:.2f} ERA")
        if starter_bits:
            notes.append("Probable starters: " + " / ".join(starter_bits))
    if starter_adjustment:
        notes.append(f"Starter adjustment: {starter_adjustment:+.2f}")
    return notes


def enrich_mlb_predictions(predictions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Upgrade baseline MLB totals with recent form and probable starter quality.

    Formula:
      1) Keep the existing season PF/PA projection as the stable baseline.
      2) When both clubs have >=5 recent games, blend 70% baseline / 30% recent form.
      3) Add a conservative probable-starter ERA signal, capped at +/-1.25 runs.

    If StatsAPI data is missing, each unavailable component simply falls back to the
    baseline instead of dropping the game or fabricating a value.
    """
    if not predictions:
        return predictions

    game_date = _prediction_date(predictions)
    season = game_date.year
    schedule_games = _schedule_games_for_date(game_date)
    recent_form = _build_recent_form(_recent_games_payload(game_date), RECENT_GAMES)

    for prediction in predictions:
        baseline = _to_float(prediction.get("predicted_total"))
        prediction["model_version"] = MODEL_VERSION
        prediction["baseline_total"] = baseline
        prediction["recent_total"] = None
        prediction["starter_adjustment"] = 0.0
        prediction["away_recent"] = None
        prediction["home_recent"] = None
        prediction["away_starter"] = None
        prediction["home_starter"] = None
        prediction["model_notes"] = []

        if baseline is None:
            continue

        away_name = prediction.get("away_team_raw") or prediction.get("team1")
        home_name = prediction.get("home_team_raw") or prediction.get("team2")
        away_form = _find_recent_form(away_name, recent_form)
        home_form = _find_recent_form(home_name, recent_form)
        recent_total = _recent_projection(away_form, home_form)

        schedule_game = _match_schedule_game(prediction, schedule_games)
        away_starter = home_starter = None
        if schedule_game:
            teams = schedule_game.get("teams") or {}
            away_starter = _starter_with_stats(teams.get("away") or {}, season)
            home_starter = _starter_with_stats(teams.get("home") or {}, season)

        starter_adjustment = _starter_adjustment(away_starter, home_starter)
        blended = baseline
        if recent_total is not None:
            weight = max(0.0, min(0.75, RECENT_WEIGHT))
            blended = baseline * (1.0 - weight) + recent_total * weight

        final_total = round(blended + starter_adjustment, 1)
        prediction.update(
            {
                "predicted_total": final_total,
                "recent_total": recent_total,
                "starter_adjustment": starter_adjustment,
                "away_recent": away_form,
                "home_recent": home_form,
                "away_starter": away_starter,
                "home_starter": home_starter,
                "model_notes": _build_notes(
                    recent_total, starter_adjustment, away_starter, home_starter
                ),
            }
        )

    return predictions
