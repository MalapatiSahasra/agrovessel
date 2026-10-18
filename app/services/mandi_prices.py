"""Live mandi (wholesale market) prices from data.gov.in's daily mandi price dataset.

Prices in this dataset are quoted in Rs. per quintal (100 kg); we convert to Rs/kg
for the decision engine. The dataset is refreshed daily but only supports an exact
match per filter field, so we issue one request per state and merge results.
"""

import asyncio
from collections import defaultdict
from datetime import datetime

import httpx

from app.config import settings

BASE_URL = "https://api.data.gov.in/resource"


LAST_ERROR: str | None = None  # most recent data.gov.in failure, for /api/price-check


async def _fetch_state(client: httpx.AsyncClient, commodity: str, state: str) -> list[dict]:
    global LAST_ERROR
    url = f"{BASE_URL}/{settings.data_gov_in_resource_id}"
    params = {
        "api-key": settings.data_gov_in_api_key,
        "format": "json",
        "limit": "500",
        "filters[commodity]": commodity,
        "filters[state]": state,
    }
    try:
        resp = await client.get(url, params=params, timeout=settings.request_timeout_seconds)
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            # e.g. {"error": "Key not authorised"} for an invalid API key
            LAST_ERROR = f"HTTP {resp.status_code}: {data.get('error', 'unknown error')}"
            return []
        LAST_ERROR = None
        return data.get("records", [])
    except (httpx.HTTPError, ValueError) as exc:
        LAST_ERROR = f"{type(exc).__name__}: {exc}"
        return []


def _parse_date(value: str) -> datetime | None:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


async def get_latest_prices(commodity: str, states: list[str]) -> dict[str, dict]:
    """Returns {market_name_lower: {market, state, district, price_per_kg, arrival_date}}
    keeping only the most recent record per market."""
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_fetch_state(client, commodity, state) for state in states])

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
            arrival_date = rec.get("arrival_date", "")
            parsed = _parse_date(arrival_date)
            key = market.lower()
            existing = latest.get(key)
            if existing is None or (parsed and (existing["_parsed"] is None or parsed > existing["_parsed"])):
                latest[key] = {
                    "market": market,
                    "state": rec.get("state", ""),
                    "district": rec.get("district", ""),
                    "price_per_kg": price_per_kg,
                    "arrival_date": arrival_date,
                    "_parsed": parsed,
                }
    return latest
