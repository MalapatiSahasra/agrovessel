"""Live mandi (wholesale market) prices from data.gov.in's daily mandi price dataset
(Agmarknet "Current Daily Price of Various Commodities from Various Markets").

We fetch every record for the crop across ALL of India (no state filter) and
let the optimiser keep the ones within driving range. Prices are quoted in
Rs. per quintal (100 kg); we convert to Rs/kg.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

from app.config import settings
from app.services.geo import canonical_state

BASE_URL = "https://api.data.gov.in/resource"
_PAGE = 1000
_MAX_PAGES = 10

LAST_ERROR: str | None = None  # most recent data.gov.in failure, for /api/price-check

# Agmarknet's commodity names differ from everyday names (and the API's
# filters need an exact match), so each crop maps to the names it may be
# listed under. All of them are queried and merged.
COMMODITY_NAMES: dict[str, list[str]] = {
    "tomato": ["Tomato"],
    "onion": ["Onion"],
    "potato": ["Potato"],
    "brinjal": ["Brinjal"],
    "eggplant": ["Brinjal"],
    "cabbage": ["Cabbage"],
    "cauliflower": ["Cauliflower"],
    "okra": ["Bhindi(Ladies Finger)"],
    "bhindi": ["Bhindi(Ladies Finger)"],
    "ladies finger": ["Bhindi(Ladies Finger)"],
    "green chilli": ["Green Chilli"],
    "chilli": ["Green Chilli"],
    "carrot": ["Carrot"],
    "cucumber": ["Cucumbar(Kheera)", "Cucumber(Kheera)", "Cucumber"],
    "banana": ["Banana", "Banana - Green"],
    "mango": ["Mango", "Mango (Raw-Ripe)"],
    "grapes": ["Grapes"],
    "papaya": ["Papaya"],
    "spinach": ["Spinach"],
    "coriander": ["Coriander(Leaves)"],
    "beans": ["Beans", "French Beans (Frasbean)"],
    "french bean": ["French Beans (Frasbean)", "Beans"],
    "peas": ["Peas Wet", "Green Peas"],
    "green peas": ["Peas Wet", "Green Peas"],
    "garlic": ["Garlic"],
    "ginger": ["Ginger(Green)"],
    "watermelon": ["Water Melon"],
    "water melon": ["Water Melon"],
    "pomegranate": ["Pomegranate"],
}


def commodity_names(crop: str) -> list[str]:
    key = crop.strip().lower()
    return COMMODITY_NAMES.get(key, [crop.strip().title()])


async def _fetch_page(client: httpx.AsyncClient, commodity: str, offset: int) -> tuple[list[dict], int]:
    """One page of records and the dataset's reported total for this filter."""
    global LAST_ERROR
    url = f"{BASE_URL}/{settings.data_gov_in_resource_id}"
    params = {
        "api-key": settings.data_gov_in_api_key,
        "format": "json",
        "limit": str(_PAGE),
        "offset": str(offset),
        "filters[commodity]": commodity,
    }
    try:
        resp = await client.get(url, params=params, timeout=settings.data_gov_in_timeout_seconds)
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            # e.g. {"error": "Key not authorised"} for an invalid API key
            LAST_ERROR = f"HTTP {resp.status_code}: {data.get('error', 'unknown error')}"
            return [], 0
        LAST_ERROR = None
        records = data.get("records", []) or []
        try:
            total = int(data.get("total", len(records)))
        except (TypeError, ValueError):
            total = len(records)
        return records, total
    except (httpx.HTTPError, ValueError) as exc:
        LAST_ERROR = f"{type(exc).__name__}: {exc}"
        return [], 0


async def _fetch_commodity(client: httpx.AsyncClient, commodity: str) -> list[dict]:
    first, total = await _fetch_page(client, commodity, 0)
    if total <= len(first):
        return first
    offsets = range(_PAGE, min(total, _PAGE * _MAX_PAGES), _PAGE)
    rest = await asyncio.gather(*[_fetch_page(client, commodity, o) for o in offsets])
    out = list(first)
    for recs, _ in rest:
        out.extend(recs)
    return out


def _parse_date(value: str) -> datetime | None:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


async def get_latest_prices(crop: str) -> dict[str, dict]:
    """All-India prices for a crop:
    {"<market>|<state>": {market, state, district, price_per_kg, arrival_date}},
    keeping only the most recent record per market."""
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_fetch_commodity(client, c) for c in commodity_names(crop)])

    latest: dict[str, dict] = {}
    for records in results:
        for rec in records:
            market = (rec.get("market") or "").strip()
            modal_price = rec.get("modal_price")
            if not market or not modal_price:
                continue
            try:
                price_per_kg = float(modal_price) / 100.0
            except (TypeError, ValueError):
                continue
            if price_per_kg <= 0:
                continue
            state = canonical_state(rec.get("state")) or (rec.get("state") or "").strip()
            arrival_date = rec.get("arrival_date", "")
            parsed = _parse_date(arrival_date)
            key = f"{market.lower()}|{state.lower()}"
            existing = latest.get(key)
            if existing is None or (parsed and (existing["_parsed"] is None or parsed > existing["_parsed"])):
                latest[key] = {
                    "market": market,
                    "state": state,
                    "district": (rec.get("district") or "").strip(),
                    "price_per_kg": price_per_kg,
                    "arrival_date": arrival_date,
                    "_parsed": parsed,
                }
    return latest
