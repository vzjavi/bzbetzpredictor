import json
import re
from difflib import get_close_matches
from typing import Dict, List, Tuple, Optional, Iterable

_STOPWORDS = {"university", "univ", "the", "of", "and", "&"}
_PUNCT_RE = re.compile(r"[^a-z0-9]+")

# --- Alias normalization (schedule name -> canonical/stats name) ---
ALIAS_NORMALIZATION: Dict[str, str] = {
    "USC": "Southern California",
    "LSU": "Louisiana State",
    "BYU": "Brigham Young",
    "UMASS": "Massachusetts",
    "HAWAII": "Hawai'i",
    "LONG ISLAND": "LIU",
    "PORTLAND ST": "Portland State",
    "MISSOURI ST": "Missouri State",
    "CHATTANOOGA": "Chattanooga",
    "NORTH ALABAMA": "North Alabama",
    "BUCKNELL": "Bucknell",
    "DUQUESNE": "Duquesne",
    "MARSHALL": "Marshall",
}

def _normalize_alias(name: str) -> str:
    """Normalize common aliases and punctuation/case differences."""
    s = (name or "").strip()
    s = s.replace("&", "and").replace(".", "")
    s = s.replace("’", "'").replace("`", "'")
    s_up = " ".join(s.upper().split())
    return ALIAS_NORMALIZATION.get(s_up, name)

def canonicalize(name: str) -> str:
    s = name.lower().strip()
    s = _PUNCT_RE.sub(" ", s)
    parts = [p for p in s.split() if p and p not in _STOPWORDS]
    return " ".join(parts)

def load_mapping(path: str) -> Dict[str, dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_mapping(path: str, mapping: Dict[str, dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)

def _all_known_names(mapping: Dict[str, dict]) -> List[str]:
    names = []
    for k, v in mapping.items():
        names.append(k)
        for a in v.get("aliases", []):
            names.append(a)
    return names

def _best_match(team: str, candidates: Iterable[str], cutoff: float = 0.86) -> Optional[str]:
    matches = get_close_matches(team, list(candidates), n=1, cutoff=cutoff)
    return matches[0] if matches else None

def resolve_team(team_name: str, mapping: Dict[str, dict], sheet_names: Optional[Iterable[str]] = None,
                 cutoff_alias: float = 0.86, cutoff_sheet: float = 0.82) -> Tuple[str, str]:
    # Normalize common aliases first (e.g., USC -> Southern California)
    team_name = _normalize_alias(team_name)

    # Exact hit on canonical keys
    if team_name in mapping:
        return team_name, mapping[team_name].get("stats_key", team_name)

    # Alias hit
    for k, v in mapping.items():
        if team_name in v.get("aliases", []):
            return k, v.get("stats_key", k)

    # Fuzzy against known names (keys + aliases)
    known = _all_known_names(mapping)
    match = _best_match(team_name, known, cutoff_alias)
    if match:
        for k, v in mapping.items():
            if match == k or match in v.get("aliases", []):
                return k, v.get("stats_key", k)

    # Fuzzy against sheet names (as a last resort)
    if sheet_names:
        match2 = _best_match(team_name, sheet_names, cutoff_sheet)
        if match2:
            return team_name, match2

    # Fall back to itself (caller can decide to skip)
    return team_name, team_name

def extend_mapping_with_schedule(schedule_team_names: Iterable[str], mapping: Dict[str, dict],
                                 sheet_names: Optional[Iterable[str]] = None,
                                 default_notes: str = "Fill PF/PA/PPG from your Google Sheet or API.") -> Dict[str, dict]:
    for raw in schedule_team_names:
        if raw in mapping:
            continue
        # Skip if known as alias
        alias_hit = any(raw in v.get("aliases", []) for v in mapping.values())
        if alias_hit:
            continue
        # Try align to sheet
        resolved_stats_key = None
        if sheet_names:
            m = get_close_matches(raw, list(sheet_names), n=1, cutoff=0.86)
            if m:
                resolved_stats_key = m[0]
        mapping[raw] = {
            "aliases": [],
            "abbr": "",
            "stats_key": resolved_stats_key or raw,
            "notes": default_notes,
            "PF": None, "PA": None, "PPG": None
        }
    return mapping
