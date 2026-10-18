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
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
from app.services.geo import canonical_state, locate_mandi
from app.services.routing import get_routes, haversine_km
from app.services.weather import avg_temp_between, get_hourly_forecasts

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

# All departure/arrival times are Indian local time. The server (e.g. Vercel)
# runs in UTC, so never use a bare datetime.now().
_IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    """Current Indian time as a naive datetime (matches Open-Meteo's keys)."""
    return datetime.now(_IST).replace(tzinfo=None, second=0, microsecond=0)


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


# Words that appear in many market names and say nothing about *which*
# market it is. Matching on these would attach e.g. a Trichy "Gandhi Market"
# price to "Salem Market".
_GENERIC_WORDS = {
    "apmc", "market", "yard", "bazar", "bazaar", "mandi", "rythu", "main",
    "new", "old", "vegetable", "vegetables", "fruit", "fruits", "sub",
    "regulated", "krishi", "utpanna", "samiti", "the", "and", "city",
}


def _tokens(s: str) -> set[str]:
    return {
        w for w in re.split(r"[^a-z0-9]+", s.lower())
        if len(w) > 2 and w not in _GENERIC_WORDS
    }


def match_price_to_market(
    market: dict[str, Any],
    mandi_prices: dict[str, dict],
) -> dict | None:
    """Match a market registry entry to the best live mandi price record.

    Only records from the same state are considered (when the record has a
    state). Each record is scored:
      * +3 for every distinctive word shared between our market name and the
        mandi record's market name (generic words like "market"/"apmc" ignored)
      * +2 if the record's district equals our market's district
    The highest-scoring record wins; no shared word and no district match
    means no match.
    """
    state = canonical_state(market.get("state")) or ""
    district = _normalise(market.get("district", ""))
    name_tokens = _tokens(market.get("name", "")) | _tokens(district)

    best: dict | None = None
    best_score = 0
    for key, rec in mandi_prices.items():
        rec_state = canonical_state(rec.get("state")) or ""
        if state and rec_state and rec_state != state:
            continue
        score = 3 * len(name_tokens & _tokens(rec.get("market") or key))
        rec_district = _normalise(rec.get("district", "") or "")
        if district and rec_district == district:
            score += 2
        if score > best_score:
            best, best_score = rec, score
    return best


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

    # --- Live mandi prices for this crop, across all of India ---
    mandi_prices = await get_latest_prices(req.crop)
    olat, olon = req.origin_lat, req.origin_lon

    candidates: list[dict[str, Any]] = []
    if mandi_prices:
        # 1. Our curated markets (precise coordinates) that have a live price.
        used: set[int] = set()
        for m in MARKETS:
            dist = haversine_km(olat, olon, m["lat"], m["lon"])
            if dist > req.max_distance_km:
                continue
            rec = match_price_to_market(m, mandi_prices)
            if rec is not None and id(rec) not in used:
                used.add(id(rec))
                candidates.append({**m, "_hav": dist, "_price": rec, "_sample": False})
        # 2. Every other mandi in India reporting this crop today, placed on
        #    the map with the offline gazetteer.
        for key, rec in mandi_prices.items():
            if id(rec) in used:
                continue
            loc = locate_mandi(rec["market"], rec.get("district"), rec.get("state"))
            if loc is None:
                continue
            dist = haversine_km(olat, olon, loc[0], loc[1])
            if dist > req.max_distance_km:
                continue
            candidates.append({
                "id": re.sub(r"[^a-z0-9]+", "_", key).strip("_"),
                "name": rec["market"],
                "state": rec.get("state") or "",
                "district": rec.get("district") or "",
                "lat": loc[0],
                "lon": loc[1],
                "_hav": dist,
                "_price": rec,
                "_sample": False,
            })
    else:
        # Live prices unavailable: fall back to placeholder sample prices for
        # our curated markets only (clearly labelled as not real).
        sample_for_crop = SAMPLE_PRICES.get(req.crop.strip().lower(), {})
        for m in MARKETS:
            dist = haversine_km(olat, olon, m["lat"], m["lon"])
            sample = sample_for_crop.get(m["id"])
            if dist <= req.max_distance_km and sample:
                rec = {"price_per_kg": sample["price_per_kg"], "arrival_date": "sample"}
                candidates.append({**m, "_hav": dist, "_price": rec, "_sample": True})

    n_found_in_range = len(candidates)

    # Keep the most promising markets for detailed routing/weather: rank by a
    # rough net (price × qty minus straight-line transport cost).
    def _rough_net(c: dict[str, Any]) -> float:
        cost = compute_transport_cost(c["_hav"] * 1.3, req.truck_mileage_kmpl, diesel_price,
                                      req.toll_per_km, req.driver_fee)
        return c["_price"]["price_per_kg"] * req.quantity_kg - cost

    candidates.sort(key=_rough_net, reverse=True)
    candidates = candidates[: max(15, req.max_candidate_markets * 2)]

    n_live_priced = 0
    n_sample_priced = 0
    skipped_closed: list[str] = []

    # --- Routes (one OSRM table call) + weather (one Open-Meteo call) ---
    async with httpx.AsyncClient() as client:
        dests = [(m["lat"], m["lon"]) for m in candidates]
        routes, weathers = await asyncio.gather(
            get_routes(client, olat, olon, dests),
            get_hourly_forecasts(client, [(olat, olon), *dests]),
        )
    origin_weather = weathers[0] if weathers else {}
    dest_weathers = weathers[1:]

    # --- Evaluate every (market × departure_time) pair ---
    now = _now_ist()
    today = now
    recommendations: list[MarketRecommendation] = []

    for idx, market in enumerate(candidates):
        distance_km, duration_hr = routes[idx]
        if distance_km > req.max_distance_km:
            continue  # by road it's beyond the farmer's limit

        price_rec = market["_price"]
        is_sample = market["_sample"]
        mandi_price_per_kg = price_rec["price_per_kg"]
        price_source_date = price_rec.get("arrival_date")

        dest_weather = dest_weathers[idx] if idx < len(dest_weathers) else {}

        # Evaluate each departure time
        departure_results: list[DepartureOption] = []
        for dep_str in req.departure_options:
            dep_hour, dep_min = map(int, dep_str.split(":"))
            departure_dt = today.replace(hour=dep_hour, minute=dep_min)
            # If this departure time is already past today, project to tomorrow.
            if departure_dt < now:
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
            # Can't reach this market before it closes from any departure:
            # leave it out (it must never rank above a reachable market).
            skipped_closed.append(market["name"])
            continue
        if is_sample:
            n_sample_priced += 1
        else:
            n_live_priced += 1
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
    if n_sample_priced:
        disclaimers.append(
            "⚠️ Live mandi prices could not be fetched, so every price shown is a "
            "placeholder sample, NOT real market data. Do not use these results "
            "to decide where to sell — check actual mandi prices first."
        )
    elif mandi_prices:
        disclaimers.append(
            f"Live prices from data.gov.in (Agmarknet): {len(mandi_prices)} mandis across India "
            f"reported {req.crop.strip().title()}; {n_found_in_range} "
            f"{'is' if n_found_in_range == 1 else 'are'} within "
            f"{req.max_distance_km:.0f} km of you. Prices may be 12–24 hours old."
        )
        if not n_found_in_range:
            disclaimers.append("No mandi within your distance limit reported this crop today — try a larger max distance.")
    else:
        disclaimers.append(
            f"No live prices found for '{req.crop}' today, and there is no sample data for it."
        )
    if skipped_closed:
        disclaimers.append(
            "Left out because no chosen departure arrives before the market closes: "
            + ", ".join(skipped_closed) + "."
        )
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
