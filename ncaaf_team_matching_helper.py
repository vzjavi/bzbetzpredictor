import json
import re
from difflib import get_close_matches
from typing import Dict, List, Tuple, Optional, Iterable

# Words we can ignore when canonicalizing
_STOPWORDS = {"university", "univ", "the", "of", "and", "&"}
_PUNCT_RE = re.compile(r"[^a-z0-9]+")

def canonicalize(name: str) -> str:
    s = (name or "").lower().strip()
    s = s.replace("’", "'")
    s = s.replace("hawai'i", "hawaii")  # normalize common accent
    s = _PUNCT_RE.sub(" ", s)
    parts = [p for p in s.split() if p and p not in _STOPWORDS]
    return " ".join(parts)

def load_mapping(path: str) -> Dict[str, dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_mapping(path: str, mapping: Dict[str, dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)

def _best_match(s: str, candidates: Iterable[str], cutoff: float = 0.82) -> Optional[str]:
    matches = get_close_matches(s, list(candidates), n=1, cutoff=cutoff)
    return matches[0] if matches else None

def _build_canonical_index(mapping: Dict[str, dict]):
    """Builds lookup dicts on canonical forms."""
    canon_primary = {}
    canon_alias = {}
    for primary, meta in mapping.items():
        canon_primary[canonicalize(primary)] = primary
        for a in meta.get("aliases", []) or []:
            canon_alias[canonicalize(a)] = primary
    return canon_primary, canon_alias

# Common, tricky alias sets seen in SportsDB schedules
COMMON_ALIASES: Dict[str, List[str]] = {
    "USC": ["Southern California", "USC Trojans"],
    "LSU": ["Louisiana State", "LSU Tigers"],
    "UMass": ["Massachusetts", "Massachusetts Minutemen"],
    "BYU": ["Brigham Young", "BYU Cougars"],
    "UCLA": ["UCLA Bruins"],
    "Ole Miss": ["Mississippi", "Mississippi Rebels"],
    "Cal": ["California", "California Golden Bears"],
    "UTEP": ["Texas-El Paso", "Texas El Paso", "UTEP Miners"],
    "UTSA": ["Texas San Antonio", "UTSA Roadrunners"],
    "Louisiana": ["Louisiana-Lafayette", "ULL", "Louisiana Ragin' Cajuns"],
    "Louisiana-Monroe": ["ULM", "Louisiana Monroe"],
    "Kansas State": ["Kansas St", "K-State", "Kansas State Wildcats"],
    "Western Kentucky": ["WKU", "Western Kentucky Hilltoppers"],
    "Georgia State": ["Georgia St"],
    "Southeastern Louisiana": ["SE Louisiana", "Southeastern Louisiana Lions"],
    "Southeast Missouri State": ["SE Missouri State", "Southeast Missouri St", "SE Missouri St", "SEMO"],
    "Hawaii": ["Hawai'i", "Hawaiʻi", "Hawaii Rainbow Warriors"],
    "Arizona State": ["Arizona St"],
    "Ohio State": ["Ohio St"],
    "Florida State": ["Florida St"],
    "Penn State": ["Penn St"],
    "Texas State": ["Texas State Bobcats"],
    "Air Force": ["Air Force Falcons"],
    "Duquesne": ["Duquesne Dukes"],
    "Bucknell": ["Bucknell Bison"],
    "Portland State": ["Portland State Vikings"],
    "Northern Arizona": ["Northern Arizona Lumberjacks"],
    "North Dakota": ["North Dakota Fighting Hawks"],
    "Chattanooga": ["Chattanooga Mocs"],
}

# Fast map of abbrev => preferred primary label
ABBREV_TO_PRIMARY = {
    "USC": "USC",
    "LSU": "LSU",
    "UCLA": "UCLA",
    "BYU": "BYU",
    "UMASS": "UMass",
    "WKU": "Western Kentucky",
    "SEMO": "Southeast Missouri State",
    "ULL": "Louisiana",
    "ULM": "Louisiana-Monroe",
    "UTEP": "UTEP",
    "UTSA": "UTSA",
    "K-STATE": "Kansas State",
    "KSTATE": "Kansas State",
    "OLE MISS": "Ole Miss",
    "PENN ST": "Penn State",
    "OHIO ST": "Ohio State",
    "ARIZONA ST": "Arizona State",
    "GEORGIA ST": "Georgia State",
}

def _normalize_to_primary(team_name: str) -> Optional[str]:
    abbr = team_name.upper().replace(".", "").strip()
    return ABBREV_TO_PRIMARY.get(abbr)

def _seed_common_aliases(mapping: Dict[str, dict]) -> None:
    """Make sure COMMON_ALIASES primaries exist and aliases are merged."""
    for primary, aliases in COMMON_ALIASES.items():
        entry = mapping.get(primary, {"aliases": [], "abbr": "", "stats_key": primary, "PF": None, "PA": None, "PPG": None})
        existing = set(entry.get("aliases", []) or [])
        for a in aliases:
            if a not in existing:
                existing.add(a)
        entry["aliases"] = sorted(existing)
        if not entry.get("stats_key"):
            entry["stats_key"] = primary
        for fld in ("PF", "PA", "PPG"):
            entry.setdefault(fld, None)
        mapping[primary] = entry

def resolve_team(
    team_name: str,
    mapping: Dict[str, dict],
    sheet_names: Optional[Iterable[str]] = None,
    cutoff_alias: float = 0.82,
    cutoff_sheet: float = 0.78,
) -> Tuple[str, str]:
    """
    Returns (primary_key, stats_key).
    - Uses canonical/alias matching
    - Understands common abbreviations and accent variants
    - Optionally aligns to a provided list of sheet_names
    """
    if not team_name:
        return "", ""

    # Abbrev normalization first (USC/LSU/UMass/etc.)
    norm_primary = _normalize_to_primary(team_name)
    if norm_primary:
        if norm_primary in mapping:
            return norm_primary, mapping[norm_primary].get("stats_key", norm_primary)
        return norm_primary, norm_primary

    # Build canonical indexes
    canon_primary, canon_alias = _build_canonical_index(mapping)
    cn = canonicalize(team_name)

    # Direct canonical hit on primary/alias
    if cn in canon_primary:
        p = canon_primary[cn]
        return p, mapping[p].get("stats_key", p)
    if cn in canon_alias:
        p = canon_alias[cn]
        return p, mapping[p].get("stats_key", p)

    # Try known COMMON_ALIASES families even if not in mapping yet
    for primary, aliases in COMMON_ALIASES.items():
        fam = [primary] + aliases
        fam_canon = [canonicalize(x) for x in fam]
        if cn in fam_canon:
            if primary in mapping:
                return primary, mapping[primary].get("stats_key", primary)
            return primary, primary

    # Fuzzy against known mapping (canonical)
    known_canon = list(canon_primary.keys()) + list(canon_alias.keys())
    match = _best_match(cn, known_canon, cutoff_alias)
    if match:
        p = canon_primary.get(match) or canon_alias.get(match)
        return p, mapping[p].get("stats_key", p)

    # Optionally fuzzy against the sheet names provided
    if sheet_names:
        sheet_canon = [canonicalize(s) for s in sheet_names]
        sm = _best_match(cn, sheet_canon, cutoff_sheet)
        if sm:
            idx = sheet_canon.index(sm)
            stats_key = list(sheet_names)[idx]
            return team_name, stats_key

    # Give up: treat raw as both
    return team_name, team_name

def extend_mapping_with_schedule(
    schedule_team_names: Iterable[str],
    mapping: Dict[str, dict],
    sheet_names: Optional[Iterable[str]] = None,
    default_notes: str = "Fill PF/PA/PPG from your Google Sheet or API.",
) -> Dict[str, dict]:
    # Ensure helpful alias families exist
    _seed_common_aliases(mapping)

    for raw in schedule_team_names:
        if not raw:
            continue
        primary, stats_key_guess = resolve_team(raw, mapping, sheet_names)
        if primary not in mapping:
            mapping[primary] = {
                "aliases": [] if raw == primary else [raw],
                "abbr": "",
                "stats_key": stats_key_guess or primary,
                "notes": default_notes,
                "PF": None, "PA": None, "PPG": None,
            }
        else:
            # add the raw name as alias if helpful
            aliases = set(mapping[primary].get("aliases", []) or [])
            if raw != primary and raw not in aliases:
                aliases.add(raw)
                mapping[primary]["aliases"] = sorted(aliases)
            if not mapping[primary].get("stats_key"):
                mapping[primary]["stats_key"] = stats_key_guess or primary

    return mapping
