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
