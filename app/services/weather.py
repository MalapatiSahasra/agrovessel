"""Hourly temperature forecast from Open-Meteo (free, no API key required).

Used to estimate the average ambient temperature an open, non-refrigerated
truck will experience during transit, which drives the spoilage model.
"""

import asyncio
from datetime import datetime, timedelta

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


_HEADERS = {"User-Agent": "AgroVessel/1.0 (https://agrovessel.vercel.app; mandi decision support)"}
MET_NO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

LAST_SOURCE: str | None = None   # which service answered the last request
LAST_ERROR: str | None = None    # why Open-Meteo failed, if it did


async def _open_meteo_batch(client: httpx.AsyncClient, coords: list[tuple[float, float]]) -> list[dict]:
    params = {
        "latitude": ",".join(f"{la:.4f}" for la, _ in coords),
        "longitude": ",".join(f"{lo:.4f}" for _, lo in coords),
        "hourly": "temperature_2m",
        "timezone": "Asia/Kolkata",
        "forecast_days": 3,
    }
    resp = await client.get(settings.open_meteo_base_url, params=params,
                            timeout=settings.request_timeout_seconds, headers=_HEADERS)
    if resp.status_code != 200:
        raise httpx.HTTPStatusError(f"Open-Meteo HTTP {resp.status_code}: {resp.text[:120]}",
                                    request=resp.request, response=resp)
    data = resp.json()
    items = data if isinstance(data, list) else [data]
    out = [_parse_hourly(d) for d in items]
    if not any(out):
        raise ValueError("Open-Meteo returned no hourly data")
    return out + [{}] * (len(coords) - len(out))


async def _met_no_point(client: httpx.AsyncClient, lat: float, lon: float) -> dict[datetime, float]:
    """MET Norway locationforecast (free, global, needs a User-Agent).
    Times are UTC; convert to naive IST to match the rest of the engine."""
    try:
        resp = await client.get(MET_NO_URL, params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"},
                                timeout=settings.request_timeout_seconds, headers=_HEADERS)
        resp.raise_for_status()
        series = resp.json()["properties"]["timeseries"]
    except (httpx.HTTPError, ValueError, KeyError):
        return {}
    out: dict[datetime, float] = {}
    for item in series:
        try:
            t = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
            ist = (t + timedelta(hours=5, minutes=30)).replace(tzinfo=None)
            out[ist] = float(item["data"]["instant"]["details"]["air_temperature"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


async def get_hourly_forecasts(
    client: httpx.AsyncClient, coords: list[tuple[float, float]]
) -> list[dict[datetime, float]]:
    """Hourly temperature for many points. Tries one batched Open-Meteo request;
    if that fails (it rate-limits shared cloud IPs), falls back to MET Norway,
    one request per point in parallel. Empty dicts if both fail."""
    global LAST_SOURCE, LAST_ERROR
    if not coords:
        return []
    try:
        out = await _open_meteo_batch(client, coords)
        LAST_SOURCE, LAST_ERROR = "open-meteo", None
        return out
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        LAST_ERROR = str(exc)[:200]
    out = list(await asyncio.gather(*[_met_no_point(client, la, lo) for la, lo in coords]))
    LAST_SOURCE = "met.no" if any(out) else "none"
    return out
