import logging
import os
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, Iterable, List, Optional

import requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

SPORT_KEYS = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NFL": "americanfootball_nfl",
    "NCAAF": "americanfootball_ncaaf",
}

# These are intentionally called edge tiers, not confidence levels.
# They only describe how far BZ's projection is from the consensus market line.
EDGE_THRESHOLDS = {
    "MLB": {"lean": 0.5, "good": 0.75, "strong": 1.0},
    "NBA": {"lean": 1.5, "good": 2.5, "strong": 4.0},
    "NFL": {"lean": 1.0, "good": 2.0, "strong": 3.0},
    "NCAAF": {"lean": 1.5, "good": 2.5, "strong": 4.0},
}

TEAM_NAME_ALIASES = {
    "oakland athletics": "athletics",
    "southern california": "usc",
    "louisiana state": "lsu",
    "brigham young": "byu",
    "massachusetts": "umass",
    "mississippi": "ole miss",
    "california": "cal",
    "hawaii": "hawaii",
}

_cache: Dict[str, Any] = {}
_CACHE_TTL = timedelta(minutes=5)


def odds_api_enabled() -> bool:
    return bool(os.environ.get("THE_ODDS_API_KEY", "").strip())


def _normalize_team(name: Optional[str]) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = s.replace("’", "'").replace("ʻ", "'").replace("`", "'")
    s = re.sub(r"\buniversity of\b", "", s)
    s = re.sub(r"\buniversity\b", "", s)
    s = re.sub(r"\bst\.\b", "state", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return TEAM_NAME_ALIASES.get(s, s)


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


def _parse_commence_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_price(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _extract_consensus_total(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse featured totals, preserving individual Over/Under offers and prices."""
    lines: List[float] = []
    bookmaker_lines: List[Dict[str, Any]] = []
    offers: List[Dict[str, Any]] = []

    for bookmaker in event.get("bookmakers") or []:
        book_name = bookmaker.get("title") or bookmaker.get("key") or "Sportsbook"
        book_key = bookmaker.get("key") or book_name

        for market in bookmaker.get("markets") or []:
            if market.get("key") != "totals":
                continue

            market_points: List[float] = []
            over_offer = None
            under_offer = None

            for outcome in market.get("outcomes") or []:
                direction = str(outcome.get("name") or "").strip().upper()
                if direction not in {"OVER", "UNDER"}:
                    continue

                point = _to_float(outcome.get("point"))
                if point is None:
                    continue
                price = _to_price(outcome.get("price"))

                offer = {
                    "bookmaker": book_name,
                    "bookmaker_key": book_key,
                    "direction": direction,
                    "total": point,
                    "price": price,
                    "last_update": market.get("last_update") or bookmaker.get("last_update"),
                }
                offers.append(offer)
                market_points.append(point)
                if direction == "OVER":
                    over_offer = offer
                else:
                    under_offer = offer

            if not market_points:
                continue

            # Featured Over and Under normally share the same point. Taking the
            # median remains robust if a provider briefly returns mismatched points.
            book_total = float(median(market_points))
            lines.append(book_total)
            bookmaker_lines.append(
                {
                    "bookmaker": book_name,
                    "bookmaker_key": book_key,
                    "total": book_total,
                    "over_price": over_offer.get("price") if over_offer else None,
                    "under_price": under_offer.get("price") if under_offer else None,
                    "over_total": over_offer.get("total") if over_offer else None,
                    "under_total": under_offer.get("total") if under_offer else None,
                }
            )

    if not lines:
        return None

    return {
        "market_total": round(float(median(lines)), 1),
        "bookmaker_count": len(bookmaker_lines),
        "bookmaker_lines": bookmaker_lines,
        "offers": offers,
    }


def _select_best_offer(offers: Iterable[Dict[str, Any]], pick: str) -> Optional[Dict[str, Any]]:
    """Return the bettor-friendliest featured total for the requested direction.

    OVER prefers the lowest total; UNDER prefers the highest total. If multiple
    books offer the same point, the higher American price is better for the bettor
    (for example -105 is better than -110, and +100 is better than -105).
    """
    pick = (pick or "").upper()
    if pick not in {"OVER", "UNDER"}:
        return None

    candidates = []
    for offer in offers or []:
        if (offer.get("direction") or "").upper() != pick:
            continue
        total = _to_float(offer.get("total"))
        if total is None:
            continue
        price = _to_price(offer.get("price"))
        normalized = dict(offer)
        normalized["total"] = total
        normalized["price"] = price
        candidates.append(normalized)

    if not candidates:
        return None

    # Missing prices lose a same-line tiebreak but do not disqualify a better point.
    def price_rank(offer: Dict[str, Any]) -> int:
        return offer.get("price") if offer.get("price") is not None else -100000

    if pick == "OVER":
        candidates.sort(key=lambda offer: (offer["total"], -price_rank(offer)))
    else:
        candidates.sort(key=lambda offer: (-offer["total"], -price_rank(offer)))
    return candidates[0]


def fetch_totals_market(league: str) -> List[Dict[str, Any]]:
    api_key = os.environ.get("THE_ODDS_API_KEY", "").strip()
    sport_key = SPORT_KEYS.get(league)
    if not api_key or not sport_key:
        return []

    now = datetime.now(timezone.utc)
    cached = _cache.get(league)
    if cached:
        fetched_at, events = cached
        if now - fetched_at < _CACHE_TTL:
            return events

    params = {
        "apiKey": api_key,
        "regions": os.environ.get("ODDS_API_REGIONS", "us"),
        "markets": "totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }

    timeout = float(os.environ.get("ODDS_API_TIMEOUT", "12"))
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"

    try:
        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        raw_events = response.json() or []
        remaining = response.headers.get("x-requests-remaining")
        if remaining is not None:
            logging.info("Odds API credits remaining after %s fetch: %s", league, remaining)
    except Exception as exc:
        logging.warning("Odds API fetch failed for %s: %s", league, exc)
        return []

    parsed: List[Dict[str, Any]] = []
    for event in raw_events:
        consensus = _extract_consensus_total(event)
        if not consensus:
            continue
        parsed.append(
            {
                "event_id": event.get("id"),
                "home_team": event.get("home_team"),
                "away_team": event.get("away_team"),
                "commence_time": _parse_commence_time(event.get("commence_time")),
                **consensus,
            }
        )

    _cache[league] = (now, parsed)
    logging.info("Odds API: %s totals markets loaded for %s", len(parsed), league)
    return parsed


def _find_market_for_prediction(
    prediction: Dict[str, Any],
    markets: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    pred_home = prediction.get("home_team_raw") or prediction.get("team2")
    pred_away = prediction.get("away_team_raw") or prediction.get("team1")
    pred_time = prediction.get("game_time")

    candidates = []
    for market in markets:
        if not _teams_match(pred_home, market.get("home_team")):
            continue
        if not _teams_match(pred_away, market.get("away_team")):
            continue

        market_time = market.get("commence_time")
        time_diff = None
        if isinstance(pred_time, datetime) and isinstance(market_time, datetime):
            try:
                pred_utc = pred_time.astimezone(timezone.utc)
                time_diff = abs((pred_utc - market_time).total_seconds())
            except Exception:
                time_diff = None

        candidates.append((time_diff, market))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0] if item[0] is not None else float("inf"))
    return candidates[0][1]


def _edge_metadata(league: str, edge: float) -> Dict[str, str]:
    thresholds = EDGE_THRESHOLDS.get(
        league, {"lean": 1.0, "good": 2.0, "strong": 3.0}
    )
    magnitude = abs(edge)

    if magnitude < thresholds["lean"]:
        return {"pick": "PASS", "edge_tier": "Pass"}
    if magnitude >= thresholds["strong"]:
        tier = "Strong Edge"
    elif magnitude >= thresholds["good"]:
        tier = "Good Edge"
    else:
        tier = "Lean"

    return {
        "pick": "OVER" if edge > 0 else "UNDER",
        "edge_tier": tier,
    }


def enrich_predictions_with_odds(
    predictions: List[Dict[str, Any]],
    league: str,
) -> List[Dict[str, Any]]:
    if not predictions:
        return predictions

    markets = fetch_totals_market(league)

    for prediction in predictions:
        prediction.update(
            {
                "market_total": None,
                "edge": None,
                "abs_edge": None,
                "pick": "NO LINE",
                "edge_tier": "No market line",
                "bookmaker_count": 0,
                "best_book": None,
                "best_book_key": None,
                "best_line": None,
                "best_price": None,
                "best_edge": None,
                "best_abs_edge": None,
                "line_improvement": None,
            }
        )

        market = _find_market_for_prediction(prediction, markets)
        if not market:
            continue

        market_total = float(market["market_total"])
        predicted_total = float(prediction["predicted_total"])
        edge = round(predicted_total - market_total, 1)
        meta = _edge_metadata(league, edge)

        update = {
            "market_total": market_total,
            "edge": edge,
            "abs_edge": abs(edge),
            "pick": meta["pick"],
            "edge_tier": meta["edge_tier"],
            "bookmaker_count": market.get("bookmaker_count", 0),
            "odds_event_id": market.get("event_id"),
            "bookmaker_lines": market.get("bookmaker_lines") or [],
        }

        best_offer = _select_best_offer(market.get("offers") or [], meta["pick"])
        if best_offer:
            best_line = float(best_offer["total"])
            best_edge = round(predicted_total - best_line, 1)
            if meta["pick"] == "OVER":
                line_improvement = round(market_total - best_line, 1)
            else:
                line_improvement = round(best_line - market_total, 1)

            update.update(
                {
                    "best_book": best_offer.get("bookmaker"),
                    "best_book_key": best_offer.get("bookmaker_key"),
                    "best_line": best_line,
                    "best_price": best_offer.get("price"),
                    "best_edge": best_edge,
                    "best_abs_edge": abs(best_edge),
                    "line_improvement": line_improvement,
                }
            )

        prediction.update(update)

    return predictions


def select_best_bets(
    predictions: Iterable[Dict[str, Any]],
    limit: int = 5,
) -> List[Dict[str, Any]]:
    playable = [
        p
        for p in predictions
        if p.get("pick") in {"OVER", "UNDER"} and p.get("abs_edge") is not None
    ]
    return sorted(
        playable,
        key=lambda p: p.get("best_abs_edge") if p.get("best_abs_edge") is not None else p.get("abs_edge", 0),
        reverse=True,
    )[:limit]
