"""AgroVessel Decision Engine — Net-Profit Route Optimizer.

For every candidate market and every candidate departure time, this module
computes:
  1. Transit profile (route distance/duration from OSRM).
  2. Thermal spoilage using the Q10 model from agronomic science.
  3. Full transport cost (diesel round-trip + tolls round-trip + driver fee).
  4. Effective (markdown-adjusted) sale price.
  5. Net profit = gross revenue − transport cost.

Markets are ranked by net profit descending, and for each market the best
departure time is selected.  A "sell locally" baseline is included when the
farmer provides a local price.

References for the Q10 spoilage model:
  - UC Davis Postharvest Technology Center
  - ICAR post-harvest loss estimation guidelines
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.schemas import (
    DepartureOption,
    MarketRecommendation,
    RecommendRequest,
    RecommendResponse,
)
from app.services.fuel import get_diesel_price
from app.services.mandi_prices import get_latest_prices
from app.services.routing import get_route, haversine_km
from app.services.weather import avg_temp_between, get_hourly_forecast

# ---------------------------------------------------------------------------
# Data files (loaded once at import time)
# ---------------------------------------------------------------------------
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

with open(_DATA_DIR / "markets.json") as _f:
    MARKETS: list[dict[str, Any]] = json.load(_f)

with open(_DATA_DIR / "crop_perishability.json") as _f:
    CROP_PERISHABILITY: dict[str, dict[str, float]] = json.load(_f)

with open(_DATA_DIR / "sample_prices.json") as _f:
    SAMPLE_PRICES: dict[str, dict[str, dict]] = json.load(_f)

# Reference temperature for shelf-life values in the perishability table (°C).
_T_REF = 20.0

# Minimum spoilage from physical handling alone (even at 0 km / 0°C transit).
_HANDLING_LOSS_PCT = 2.0

# When quality degradation is significant, buyers apply a markdown to the
# modal mandi price.  These thresholds drive the markdown curve.
_MARKDOWN_THRESHOLDS: list[tuple[float, float]] = [
    # (loss_pct_lower_bound, fraction_of_modal_price_buyer_pays)
    (30.0, 0.60),   # severe damage → 60% of listed price
    (20.0, 0.72),   # heavy damage  → 72%
    (10.0, 0.85),   # moderate      → 85%
    (5.0, 0.93),    # light         → 93%
    (0.0, 1.00),    # negligible    → full price
]


# ===================================================================
# Q10 Spoilage Model
# ===================================================================

def _get_crop_params(crop: str) -> dict[str, float] | None:
    """Look up crop perishability params (case-insensitive)."""
    return CROP_PERISHABILITY.get(crop.strip().lower())


def predict_spoilage(
    crop: str,
    transit_hours: float,
    avg_temp_c: float | None,
) -> float:
    """Return predicted percentage of produce lost during transit.

    Uses the Q10 temperature-coefficient model:
        effective_shelf_life = shelf_life_ref / Q10^((T - T_ref) / 10)
        fraction_consumed    = transit_hours / effective_shelf_life

    The fraction consumed is then mapped to a loss percentage via a piecewise
    linear heuristic calibrated against ICAR field data.
    """
    params = _get_crop_params(crop)

    # If the crop isn't in our table, use a conservative generic model.
    shelf_life_ref = params["shelf_life_hr_ref"] if params else 72.0
    q10 = params["q10"] if params else 2.0

    # Default temperature assumption when weather data is unavailable.
    temp = avg_temp_c if avg_temp_c is not None else 32.0

    # Effective shelf life at the actual transit temperature.
    exponent = (temp - _T_REF) / 10.0
    effective_shelf_life = shelf_life_ref / (q10 ** exponent)

    if effective_shelf_life <= 0:
        return 50.0  # safety cap

    fraction_consumed = transit_hours / effective_shelf_life

    # Map fraction consumed → loss percentage (piecewise linear).
    if fraction_consumed < 0.05:
        loss_pct = _HANDLING_LOSS_PCT
    elif fraction_consumed < 0.10:
        loss_pct = _lerp(fraction_consumed, 0.05, 0.10, _HANDLING_LOSS_PCT, 8.0)
    elif fraction_consumed < 0.20:
        loss_pct = _lerp(fraction_consumed, 0.10, 0.20, 8.0, 15.0)
    elif fraction_consumed < 0.35:
        loss_pct = _lerp(fraction_consumed, 0.20, 0.35, 15.0, 28.0)
    else:
        loss_pct = min(50.0, _lerp(fraction_consumed, 0.35, 0.60, 28.0, 50.0))

    return round(loss_pct, 1)


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Linear interpolation, clamped to [y0, y1]."""
    t = (x - x0) / (x1 - x0) if x1 != x0 else 0.0
    t = max(0.0, min(1.0, t))
    return y0 + t * (y1 - y0)


def _markdown_factor(loss_pct: float) -> float:
    """What fraction of the modal price does the buyer actually pay?"""
    for threshold, factor in _MARKDOWN_THRESHOLDS:
        if loss_pct >= threshold:
            return factor
    return 1.0


# ===================================================================
# Transport Cost
# ===================================================================

def compute_transport_cost(
    distance_km: float,
    truck_mileage_kmpl: float,
    diesel_price: float,
    toll_per_km: float,
    driver_fee: float,
) -> float:
    """Total transport cost for a round trip."""
    round_trip_km = distance_km * 2.0
    fuel_cost = (round_trip_km / truck_mileage_kmpl) * diesel_price
    toll_cost = round_trip_km * toll_per_km
    return fuel_cost + toll_cost + driver_fee


# Time-of-day traffic heuristic: (trip-duration multiplier, effective-mileage
# multiplier). Peak-hour stop-start traffic both slows the trip down and
# burns more fuel per km; late-night driving is close to free-flow and a bit
# more fuel-efficient. Indicative only -- OSRM's demo router has no live
# traffic data, so this is a heuristic overlay, not a measured effect.
_TRAFFIC_WINDOWS: list[tuple[int, int, float, float]] = [
    # (start_hour_inclusive, end_hour_exclusive, duration_mult, mileage_mult)
    (8, 11, 1.25, 0.88),   # morning rush
    (17, 21, 1.25, 0.88),  # evening rush
    (0, 5, 0.92, 1.05),    # late night, near-empty roads
]


def _traffic_factors(hour: int) -> tuple[float, float]:
    """(duration_multiplier, effective_mileage_multiplier) for a departure hour."""
    for start, end, duration_mult, mileage_mult in _TRAFFIC_WINDOWS:
        if start <= hour < end:
            return duration_mult, mileage_mult
    return 1.0, 1.0


# ===================================================================
# Market ↔ Mandi-data fuzzy matcher
# ===================================================================

def _normalise(s: str) -> str:
    return s.strip().lower().replace(".", "").replace("-", " ")


def match_price_to_market(
    market: dict[str, Any],
    mandi_prices: dict[str, dict],
) -> dict | None:
    """Try to match a market registry entry to a live mandi price record.

    Strategy (in priority order):
      1. Exact match on market name.
      2. District name appears as a key in mandi_prices.
      3. Substring match: market-registry district name is contained in a
         mandi-data market name, or vice versa.
    """
    district = _normalise(market.get("district", ""))
    name = _normalise(market.get("name", ""))

    # 1. Exact key match on district (most mandi records are keyed by town).
    if district in mandi_prices:
        return mandi_prices[district]

    # 2. Check if any word from the market name matches a key.
    name_words = {w for w in name.split() if len(w) > 3}
    for key, rec in mandi_prices.items():
        if key == district:
            return rec
        if any(word in key for word in name_words):
            return rec
        if district and district in key:
            return rec
        # Reverse: mandi key inside our market name
        if key in name:
            return rec

    return None


# ===================================================================
# Main optimiser entry point
# ===================================================================

async def optimise(req: RecommendRequest) -> RecommendResponse:
    """Run the full optimisation loop and return ranked recommendations."""

    # --- Diesel price ---
    if req.diesel_price_per_litre is not None:
        diesel_price = req.diesel_price_per_litre
        diesel_source = "farmer-provided"
    else:
        diesel_price, diesel_source = get_diesel_price(req.origin_state)

    # --- Filter candidate markets by haversine distance ---
    candidates: list[dict[str, Any]] = []
    seen_states: set[str] = set()
    for m in MARKETS:
        dist = haversine_km(req.origin_lat, req.origin_lon, m["lat"], m["lon"])
        if dist <= req.max_distance_km:
            candidates.append({**m, "_haversine_km": dist})
            seen_states.add(m["state"])

    # Always include the farmer's own state for price fetching.
    seen_states.add(req.origin_state)

    # --- Fetch live mandi prices (all relevant states, concurrently) ---
    mandi_prices = await get_latest_prices(req.crop, list(seen_states))

    # If the live API returned no data (rate-limited, down, or no records for
    # this commodity today), fall back to our curated sample prices.
    use_sample_prices = False
    if not mandi_prices:
        use_sample_prices = True
        crop_key = req.crop.strip().lower()
        sample = SAMPLE_PRICES.get(crop_key, {})
        # Build a mandi_prices-like dict keyed by market_id for direct lookup.
        for market_id, rec in sample.items():
            mandi_prices[market_id] = {
                "market": rec.get("market", ""),
                "state": rec.get("state", ""),
                "district": rec.get("district", ""),
                "price_per_kg": rec["price_per_kg"],
                "arrival_date": "sample",
                "_parsed": None,
            }

    # --- Fetch routes + weather concurrently for all candidates ---
    async with httpx.AsyncClient() as client:
        # Routes
        route_tasks = [
            get_route(client, req.origin_lat, req.origin_lon, m["lat"], m["lon"])
            for m in candidates
        ]

        # Weather forecasts: origin + each destination (deduplicated later).
        weather_locs = [(req.origin_lat, req.origin_lon)]
        for m in candidates:
            weather_locs.append((m["lat"], m["lon"]))

        weather_tasks = [
            get_hourly_forecast(client, lat, lon)
            for lat, lon in weather_locs
        ]

        all_results = await asyncio.gather(*route_tasks, *weather_tasks, return_exceptions=True)

    # Unpack results.
    n_cand = len(candidates)
    routes = all_results[:n_cand]
    origin_weather = all_results[n_cand] if not isinstance(all_results[n_cand], Exception) else {}
    dest_weathers = []
    for i in range(n_cand):
        w = all_results[n_cand + 1 + i]
        dest_weathers.append(w if not isinstance(w, Exception) else {})

    # --- Evaluate every (market × departure_time) pair ---
    today = datetime.now().replace(second=0, microsecond=0)
    recommendations: list[MarketRecommendation] = []

    for idx, market in enumerate(candidates):
        # Route data
        if isinstance(routes[idx], Exception):
            continue
        distance_km, duration_hr = routes[idx]

        # Price data — try direct market_id lookup first (used by sample
        # prices), then fall back to fuzzy name matching (used by live API).
        price_rec = mandi_prices.get(market["id"]) or match_price_to_market(market, mandi_prices)
        if price_rec is None:
            continue  # no price data → can't recommend

        mandi_price_per_kg = price_rec["price_per_kg"]
        price_source_date = price_rec.get("arrival_date")

        # Weather data for this destination
        dest_weather = dest_weathers[idx]

        # Evaluate each departure time
        departure_results: list[DepartureOption] = []
        for dep_str in req.departure_options:
            dep_hour, dep_min = map(int, dep_str.split(":"))
            departure_dt = today.replace(hour=dep_hour, minute=dep_min)
            # If this departure time is already past today, project to tomorrow.
            if departure_dt < datetime.now():
                departure_dt += timedelta(days=1)

            duration_mult, _mileage_mult = _traffic_factors(dep_hour)
            adj_duration_hr = duration_hr * duration_mult

            arrival_dt = departure_dt + timedelta(hours=adj_duration_hr)
            arrival_str = arrival_dt.strftime("%H:%M")

            # Check market closing constraint.
            arrival_hour_decimal = arrival_dt.hour + arrival_dt.minute / 60.0
            if arrival_hour_decimal > req.market_closes_hour:
                departure_results.append(DepartureOption(
                    departure_time=dep_str,
                    arrival_time=arrival_str,
                    avg_transit_temp_c=None,
                    predicted_loss_pct=0.0,
                    feasible=False,
                    reason=f"Arrives at {arrival_str}, after market closes at {req.market_closes_hour}:00",
                ))
                continue

            # Average transit temperature (mean of origin & destination weather).
            origin_temp = avg_temp_between(origin_weather, departure_dt, arrival_dt) if origin_weather else None
            dest_temp = avg_temp_between(dest_weather, departure_dt, arrival_dt) if dest_weather else None
            if origin_temp is not None and dest_temp is not None:
                avg_temp = (origin_temp + dest_temp) / 2.0
            elif origin_temp is not None:
                avg_temp = origin_temp
            elif dest_temp is not None:
                avg_temp = dest_temp
            else:
                avg_temp = None

            # Spoilage prediction (traffic-adjusted transit time).
            loss_pct = predict_spoilage(req.crop, adj_duration_hr, avg_temp)

            # Reason text.
            if avg_temp is not None:
                if avg_temp < 25:
                    reason = f"Cool transit ({avg_temp:.0f}°C) — low spoilage risk"
                elif avg_temp < 32:
                    reason = f"Moderate heat ({avg_temp:.0f}°C) — some spoilage expected"
                else:
                    reason = f"Hot transit ({avg_temp:.0f}°C) — high spoilage risk"
            else:
                reason = "Weather data unavailable — using conservative estimate"
            if duration_mult > 1.0:
                reason += "; peak-hour traffic slows the trip and raises fuel cost"
            elif duration_mult < 1.0:
                reason += "; light night traffic, faster and more fuel-efficient"

            departure_results.append(DepartureOption(
                departure_time=dep_str,
                arrival_time=arrival_str,
                avg_transit_temp_c=round(avg_temp, 1) if avg_temp is not None else None,
                predicted_loss_pct=loss_pct,
                feasible=True,
                reason=reason,
            ))

        # Pick the best feasible departure (highest net profit). Transport cost
        # is recomputed per departure using that hour's traffic-adjusted
        # effective mileage, so peak-hour departures correctly cost more.
        best_dep: DepartureOption | None = None
        best_net_profit = -math.inf
        best_transport = 0.0
        best_duration_hr = duration_hr

        for dep in departure_results:
            if not dep.feasible:
                continue
            dep_duration_mult, dep_mileage_mult = _traffic_factors(int(dep.departure_time.split(":")[0]))
            effective_mileage = req.truck_mileage_kmpl * dep_mileage_mult
            loss_pct = dep.predicted_loss_pct
            usable_kg = req.quantity_kg * (1.0 - loss_pct / 100.0)
            eff_price = mandi_price_per_kg * _markdown_factor(loss_pct)
            gross = usable_kg * eff_price
            transport = compute_transport_cost(
                distance_km, effective_mileage,
                diesel_price, req.toll_per_km, req.driver_fee,
            )
            net = gross - transport
            if net > best_net_profit:
                best_net_profit = net
                best_dep = dep
                best_transport = transport
                best_duration_hr = duration_hr * dep_duration_mult

        if best_dep is None:
            # No feasible departure — still include with a note.
            notes = ["No feasible departure time — arrives after market closes for all options"]
            usable_kg = req.quantity_kg
            lost_kg = 0.0
            effective_price = mandi_price_per_kg
            gross = 0.0
            transport = compute_transport_cost(
                distance_km, req.truck_mileage_kmpl,
                diesel_price, req.toll_per_km, req.driver_fee,
            )
            net = 0.0
        else:
            loss_pct = best_dep.predicted_loss_pct
            usable_kg = req.quantity_kg * (1.0 - loss_pct / 100.0)
            lost_kg = req.quantity_kg - usable_kg
            effective_price = mandi_price_per_kg * _markdown_factor(loss_pct)
            gross = usable_kg * effective_price
            transport = best_transport
            net = gross - transport
            notes = []
            if best_dep.avg_transit_temp_c is not None and best_dep.avg_transit_temp_c < 25:
                notes.append("🌙 Night departure recommended for perishable cargo")
            if loss_pct > 15:
                notes.append("⚠️ High spoilage risk — consider refrigerated transport")
            if loss_pct > 5:
                notes.append(f"Quality markdown applied: buyer pays {_markdown_factor(loss_pct)*100:.0f}% of listed price")

        recommendations.append(MarketRecommendation(
            market_id=market["id"],
            market_name=market["name"],
            state=market["state"],
            is_local=False,
            distance_km=round(distance_km, 1),
            duration_hr=round(best_duration_hr, 1),
            mandi_price_per_kg=round(mandi_price_per_kg, 2),
            price_source_date=price_source_date,
            best_departure=best_dep,
            usable_kg=round(usable_kg, 0),
            lost_kg=round(lost_kg, 0),
            effective_price_per_kg=round(effective_price, 2),
            gross_revenue=round(gross, 0),
            transport_cost=round(transport, 0),
            net_profit=round(net, 0),
            notes=notes,
        ))

    # --- Add local-market baseline if price is provided ---
    if req.local_price_per_kg is not None:
        local_net = req.quantity_kg * req.local_price_per_kg
        recommendations.append(MarketRecommendation(
            market_id="__local__",
            market_name="Your Local Market",
            state=req.origin_state,
            is_local=True,
            distance_km=0.0,
            duration_hr=0.0,
            mandi_price_per_kg=req.local_price_per_kg,
            price_source_date=None,
            best_departure=DepartureOption(
                departure_time="now",
                arrival_time="now",
                avg_transit_temp_c=None,
                predicted_loss_pct=0.0,
                feasible=True,
                reason="No transit required",
            ),
            usable_kg=req.quantity_kg,
            lost_kg=0.0,
            effective_price_per_kg=req.local_price_per_kg,
            gross_revenue=round(local_net, 0),
            transport_cost=0.0,
            net_profit=round(local_net, 0),
            notes=["Baseline: selling at your local market with no transport cost"],
        ))

    # --- Sort by net profit descending ---
    recommendations.sort(key=lambda r: r.net_profit, reverse=True)

    # Trim to max_candidate_markets (but always keep local baseline if present).
    local_rec = [r for r in recommendations if r.is_local]
    non_local = [r for r in recommendations if not r.is_local]
    trimmed = non_local[: req.max_candidate_markets]
    if local_rec and local_rec[0] not in trimmed:
        trimmed.append(local_rec[0])

    # Re-sort after possible re-addition.
    trimmed.sort(key=lambda r: r.net_profit, reverse=True)

    best_id = trimmed[0].market_id if trimmed else None

    disclaimers = []
    if use_sample_prices:
        disclaimers.append(
            "⚠️ Live mandi prices could not be fetched (API may be slow/down). "
            "Using approximate sample prices — verify actual prices before travelling."
        )
    else:
        disclaimers.append("Mandi prices are from data.gov.in and may be 12–24 hours old.")
    disclaimers.extend([
        "Spoilage estimates are modelled — actual losses depend on produce quality, packing, and vehicle type.",
        "Transport costs assume round-trip diesel and tolls. Actual toll amounts may vary.",
        f"Diesel price used: ₹{diesel_price}/L ({diesel_source}).",
    ])

    return RecommendResponse(
        crop=req.crop,
        quantity_kg=req.quantity_kg,
        diesel_price_per_litre=diesel_price,
        diesel_price_source=diesel_source,
        recommendations=trimmed,
        best_market_id=best_id,
        disclaimers=disclaimers,
    )
