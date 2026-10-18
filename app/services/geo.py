"""Offline geography for all of India: state names and mandi locations.

data.gov.in gives each mandi price record a market name, district and state,
but no coordinates. To route a truck there we need a lat/lon, so we look the
mandi up in a bundled gazetteer of ~3,400 Indian towns (GeoNames cities1000,
CC BY 4.0). No network call, so it works the same on every request.

Lookup order for a record:
  1. a town with the market's name in the same state   ("Kolar")
  2. a town named after any distinct part of the name  ("Binny Mill (F&V), Bangalore" -> Bangalore)
  3. a town with the district's name in the same state
  4. the centre of all gazetteer towns in that district
Anything still unknown is skipped rather than guessed.
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from app.services.routing import haversine_km

# The 28 states and 8 union territories, in the spelling the app uses.
STATES: list[str] = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal",
    "Andaman and Nicobar", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir",
    "Ladakh", "Lakshadweep", "Puducherry",
]

# Other spellings seen in Agmarknet / data.gov.in and elsewhere.
_STATE_ALIASES = {
    "chattisgarh": "Chhattisgarh",
    "chhatisgarh": "Chhattisgarh",
    "uttrakhand": "Uttarakhand",
    "uttaranchal": "Uttarakhand",
    "orissa": "Odisha",
    "nct of delhi": "Delhi",
    "new delhi": "Delhi",
    "pondicherry": "Puducherry",
    "jammu & kashmir": "Jammu and Kashmir",
    "j&k": "Jammu and Kashmir",
    "andaman & nicobar": "Andaman and Nicobar",
    "andaman and nicobar islands": "Andaman and Nicobar",
    "dadra and nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
    "daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "telengana": "Telangana",
}
_STATE_LOOKUP = {s.lower(): s for s in STATES} | _STATE_ALIASES

# Words in mandi names that don't identify a place.
_NOISE = {
    "apmc", "market", "yard", "mandi", "bazar", "bazaar", "f&v", "fv", "veg",
    "vegetable", "vegetables", "fruit", "fruits", "main", "sub", "new", "old",
    "regulated", "krishi", "upaj", "utpanna", "samiti", "rythu", "uzhavar",
    "sandhai", "grain", "cotton", "onion", "potato", "tomato", "the", "and",
    "city", "town", "nagar", "road", "rd", "gunj", "ganj", "division",
    "district", "dist",
}


def canonical_state(name: str | None) -> str | None:
    if not name:
        return None
    key = re.sub(r"\s+", " ", name.strip().lower())
    return _STATE_LOOKUP.get(key)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _strip_noise(s: str) -> str:
    return " ".join(w for w in _norm(s).split() if w not in _NOISE)


# ---------------------------------------------------------------------------
# Gazetteer (loaded once)
# ---------------------------------------------------------------------------
_PLACES_FILE = Path(__file__).resolve().parent.parent / "data" / "india_places.json"
with open(_PLACES_FILE, encoding="utf-8") as _f:
    _PLACES: list[list] = json.load(_f)["places"]  # [name, state, district, lat, lon]

_TOWN: dict[tuple[str, str], tuple[float, float]] = {}
_DISTRICT_PTS: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
for _name, _state, _district, _lat, _lon in _PLACES:
    _TOWN.setdefault((_state, _norm(_name)), (_lat, _lon))
    if _district:
        _DISTRICT_PTS[(_state, _strip_noise(_district))].append((_lat, _lon))
_DISTRICT: dict[tuple[str, str], tuple[float, float]] = {
    k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v))
    for k, v in _DISTRICT_PTS.items()
}

_TOWNS_BY_STATE: dict[str, list[str]] = defaultdict(list)
for (_st, _n) in _TOWN:
    _TOWNS_BY_STATE[_st].append(_n)
_DISTRICTS_BY_STATE: dict[str, list[str]] = defaultdict(list)
for (_st, _d) in _DISTRICT:
    _DISTRICTS_BY_STATE[_st].append(_d)


def _fuzzy(name: str, options: list[str], cutoff: float) -> str | None:
    """Closest spelling (Agmarknet writes Dehradoon, Madanapalli, Chittor...)."""
    if len(name) < 4:
        return None
    hit = difflib.get_close_matches(name, options, n=1, cutoff=cutoff)
    return hit[0] if hit else None


# Official/Agmarknet district names that differ from the gazetteer's.
_DISTRICT_ALIASES = {
    "hooghly": "hugli", "howrah": "haora", "darjeeling": "darjiling",
    "cooch behar": "koch bihar", "malda": "maldah", "purulia": "puruliya",
    "bardhaman": "barddhaman", "burdwan": "barddhaman",
    "south 24 parganas": "south 24 paraganas",
    "gurgaon": "gurgaon", "gurugram": "gurgaon",
    "bangalore urban": "bangalore", "bengaluru": "bangalore",
    "mysuru": "mysore", "belagavi": "belgaum", "kalaburagi": "gulbarga",
    "prayagraj": "allahabad",
}

_CACHE: dict[tuple[str, str, str], tuple[float, float] | None] = {}


def locate_mandi(market: str, district: str | None, state: str | None) -> tuple[float, float] | None:
    """Best-effort (lat, lon) for a mandi, or None if we can't place it."""
    st = canonical_state(state)
    if st is None:
        return None
    if st == "Ladakh":
        st_keys = ["Ladakh", "Jammu and Kashmir"]  # gazetteer files Ladakh under J&K
    else:
        st_keys = [st]
    cache_key = (market or "", district or "", st)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    names: list[str] = []
    full = _norm(market)
    names.append(full)
    names.append(_strip_noise(market))
    for part in re.split(r"[(),/\-]+", market or ""):
        names.append(_strip_noise(part))
    names.extend(_strip_noise(market).split())
    names.append(_strip_noise(district or ""))

    found = None
    for st_key in st_keys:
        for n in names:
            if n and (st_key, n) in _TOWN:
                found = _TOWN[(st_key, n)]
                break
        if found is None:
            for n in names:
                hit = _fuzzy(n, _TOWNS_BY_STATE[st_key], 0.84)
                if hit:
                    found = _TOWN[(st_key, hit)]
                    break
        if found is None and district:
            d = _strip_noise(district)
            d = _DISTRICT_ALIASES.get(d, d)
            hit = d if (st_key, d) in _DISTRICT else _fuzzy(d, _DISTRICTS_BY_STATE[st_key], 0.8)
            if hit:
                found = _DISTRICT[(st_key, hit)]
        if found:
            break

    _CACHE[cache_key] = found
    return found


def nearest_state(lat: float, lon: float) -> str | None:
    """State of the nearest gazetteer town (for "Use my location")."""
    best, best_d = None, float("inf")
    for _name, state, _district, plat, plon in _PLACES:
        d = haversine_km(lat, lon, plat, plon)
        if d < best_d:
            best, best_d = state, d
    return best if best_d < 150 else None
