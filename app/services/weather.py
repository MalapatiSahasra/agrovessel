"""Hourly temperature forecast from Open-Meteo (free, no API key required).

Used to estimate the average ambient temperature an open, non-refrigerated
truck will experience during transit, which drives the spoilage model.
"""

from datetime import datetime

import httpx

from app.config import settings


async def get_hourly_forecast(client: httpx.AsyncClient, lat: float, lon: float) -> dict[datetime, float]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "timezone": "Asia/Kolkata",
        "forecast_days": 2,
    }
    try:
        resp = await client.get(settings.open_meteo_base_url, params=params, timeout=settings.request_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return {}

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    out: dict[datetime, float] = {}
    for t, temp in zip(times, temps):
        try:
            out[datetime.fromisoformat(t)] = float(temp)
        except (ValueError, TypeError):
            continue
    return out


def avg_temp_between(hourly: dict[datetime, float], start: datetime, end: datetime) -> float | None:
    relevant = [temp for dt, temp in hourly.items() if start <= dt <= end]
    if not relevant:
        # fall back to nearest single hour if the window is shorter than 1h resolution
        nearest = min(hourly.items(), key=lambda kv: abs((kv[0] - start).total_seconds()), default=None)
        return nearest[1] if nearest else None
    return sum(relevant) / len(relevant)


def _parse_hourly(data: dict) -> dict[datetime, float]:
    hourly = data.get("hourly", {}) or {}
    out: dict[datetime, float] = {}
    for t, temp in zip(hourly.get("time", []), hourly.get("temperature_2m", [])):
        try:
            out[datetime.fromisoformat(t)] = float(temp)
        except (ValueError, TypeError):
            continue
    return out


async def get_hourly_forecasts(
    client: httpx.AsyncClient, coords: list[tuple[float, float]]
) -> list[dict[datetime, float]]:
    """Hourly forecasts for many points in ONE Open-Meteo request (it accepts
    comma-separated latitude/longitude lists). Empty dicts on failure."""
    if not coords:
        return []
    params = {
        "latitude": ",".join(f"{la:.4f}" for la, _ in coords),
        "longitude": ",".join(f"{lo:.4f}" for _, lo in coords),
        "hourly": "temperature_2m",
        "timezone": "Asia/Kolkata",
        "forecast_days": 2,
    }
    try:
        resp = await client.get(settings.open_meteo_base_url, params=params, timeout=settings.request_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return [{} for _ in coords]
    items = data if isinstance(data, list) else [data]
    out = [_parse_hourly(d) for d in items]
    return out + [{}] * (len(coords) - len(out))
