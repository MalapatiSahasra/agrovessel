"""Road distance/duration from OSRM's public demo routing server (free, no key).

The demo server is rate-limited and best-effort; for production use, self-host
OSRM or swap in a commercial routing API without changing the call signature.
"""

import math

import httpx

from app.config import settings


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def get_route(
    client: httpx.AsyncClient, lat1: float, lon1: float, lat2: float, lon2: float
) -> tuple[float, float]:
    """Returns (distance_km, duration_hr). Falls back to a straight-line estimate
    (x1.3 for road indirectness, 40 km/h average) if OSRM is unreachable."""
    url = f"{settings.osrm_base_url}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
    try:
        resp = await client.get(url, params={"overview": "false"}, timeout=settings.request_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
        route = data["routes"][0]
        return route["distance"] / 1000.0, route["duration"] / 3600.0
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        straight = haversine_km(lat1, lon1, lat2, lon2)
        distance_km = straight * 1.3
        return distance_km, distance_km / 40.0
