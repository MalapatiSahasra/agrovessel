"""Unit tests for the AgroVessel decision engine (no network needed).

Run with:  python -m pytest -q
"""

import asyncio
import math

import httpx
import pytest

from app.engine import optimizer
from app.engine.optimizer import (
    _markdown_factor,
    _traffic_factors,
    compute_transport_cost,
    match_price_to_market,
    predict_spoilage,
)
from app.schemas import RecommendRequest
from app.services.fuel import get_diesel_price
from app.services.geo import STATES, canonical_state, locate_mandi, nearest_state
from app.services.mandi_prices import commodity_names


# --------------------------------------------------------------------- spoilage
def test_spoilage_floor_is_handling_loss():
    assert predict_spoilage("tomato", 0.1, 20.0) == 2.0


def test_spoilage_rises_with_temperature_and_time():
    cool = predict_spoilage("tomato", 6, 18)
    hot = predict_spoilage("tomato", 6, 36)
    longer = predict_spoilage("tomato", 12, 36)
    assert cool < hot < longer


def test_spoilage_is_capped():
    assert predict_spoilage("coriander", 100, 45) <= 50.0


def test_hardy_crop_spoils_less_than_leafy_crop():
    assert predict_spoilage("onion", 8, 32) < predict_spoilage("spinach", 8, 32)


def test_markdown_factor_monotonic():
    factors = [_markdown_factor(x) for x in (0, 5, 10, 20, 30, 45)]
    assert factors == sorted(factors, reverse=True)


# ------------------------------------------------------------------- transport
def test_transport_cost_round_trip():
    # 100 km one way -> 200 km; 200/10 km/l * 100 Rs = 2000; toll 200*2 = 400; driver 1500
    assert compute_transport_cost(100, 10, 100, 2, 1500) == pytest.approx(3900)


def test_traffic_windows():
    assert _traffic_factors(9)[0] > 1.0     # morning rush
    assert _traffic_factors(3)[0] < 1.0     # night
    assert _traffic_factors(14) == (1.0, 1.0)


# -------------------------------------------------------------- price matching
def test_matcher_ignores_generic_words():
    salem = {"name": "Salem Market", "state": "Tamil Nadu", "district": "Salem"}
    prices = {"gandhi market (trichy)|tamil nadu": {
        "market": "Gandhi Market (Trichy)", "state": "Tamil Nadu", "district": "Trichy", "price_per_kg": 40}}
    assert match_price_to_market(salem, prices) is None


def test_matcher_respects_state():
    kolar = {"name": "Kolar APMC", "state": "Karnataka", "district": "Kolar"}
    prices = {"kolar|andhra pradesh": {
        "market": "Kolar", "state": "Andhra Pradesh", "district": "X", "price_per_kg": 9}}
    assert match_price_to_market(kolar, prices) is None


def test_matcher_finds_same_market():
    kolar = {"name": "Kolar APMC", "state": "Karnataka", "district": "Kolar"}
    rec = {"market": "Kolar", "state": "Karnataka", "district": "Kolar", "price_per_kg": 9}
    assert match_price_to_market(kolar, {"kolar|karnataka": rec}) is rec


# ------------------------------------------------------------------- geography
def test_all_states_and_uts_listed():
    assert len(STATES) == 36


@pytest.mark.parametrize("alias,canon", [
    ("Chattisgarh", "Chhattisgarh"), ("Uttrakhand", "Uttarakhand"),
    ("NCT of Delhi", "Delhi"), ("Orissa", "Odisha"), ("Pondicherry", "Puducherry"),
])
def test_state_aliases(alias, canon):
    assert canonical_state(alias) == canon


@pytest.mark.parametrize("market,district,state", [
    ("Kolar", "Kolar", "Karnataka"),
    ("Dehradoon", "Dehradoon", "Uttrakhand"),          # Agmarknet spelling
    ("Binny Mill (F&V), Bangalore", "Bangalore", "Karnataka"),
    ("Sheoraphuly", "Hooghly", "West Bengal"),          # district alias
    ("Aluva", "Ernakulam", "Kerala"),
])
def test_locate_mandi(market, district, state):
    loc = locate_mandi(market, district, state)
    assert loc is not None
    lat, lon = loc
    assert 6 < lat < 37 and 68 < lon < 98  # inside India's bounding box


def test_nearest_state():
    assert nearest_state(9.98, 76.28) == "Kerala"
    assert nearest_state(28.99, 77.02) == "Haryana"


def test_diesel_known_for_every_state():
    for st in STATES:
        price, source = get_diesel_price(st)
        assert 70 < price < 130
        assert "default" not in source


def test_commodity_names_map_to_agmarknet():
    assert commodity_names("okra") == ["Bhindi(Ladies Finger)"]
    assert commodity_names("Tomato") == ["Tomato"]


# ------------------------------------------------- end-to-end with mocked APIs
def _mock_transport(records):
    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if "data.gov.in" in host:
            return httpx.Response(200, json={"total": len(records), "records": records})
        if "osrm" in host:
            pts = [tuple(map(float, c.split(","))) for c in req.url.path.split("/")[-1].split(";")]
            lon0, lat0 = pts[0]
            from app.services.routing import haversine_km
            d = [0.0] + [haversine_km(lat0, lon0, la, lo) * 1300 for lo, la in pts[1:]]
            t = [0.0] + [x / 1000 / 45 * 3600 for x in d[1:]]
            return httpx.Response(200, json={"code": "Ok", "distances": [d], "durations": [t]})
        if "open-meteo" in host:
            from datetime import datetime, timedelta
            n = len(req.url.params["latitude"].split(","))
            base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            times = [(base + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(72)]
            temps = [24 + 6 * math.sin((h % 24 - 9) / 24 * 2 * math.pi) for h in range(72)]
            one = {"hourly": {"time": times, "temperature_2m": temps}}
            return httpx.Response(200, json=[one] * n if n > 1 else one)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_end_to_end_ranks_by_net_profit(monkeypatch):
    records = [
        {"state": "Kerala", "district": "Ernakulam", "market": "Aluva", "modal_price": "3200", "arrival_date": "17/10/2026"},
        {"state": "Kerala", "district": "Thrissur", "market": "Thrissur", "modal_price": "3400", "arrival_date": "17/10/2026"},
        {"state": "Punjab", "district": "Ludhiana", "market": "Ludhiana", "modal_price": "9900", "arrival_date": "17/10/2026"},
    ]
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real_client(transport=_mock_transport(records)))
    req = RecommendRequest(crop="Tomato", quantity_kg=1000, origin_lat=9.98, origin_lon=76.28,
                           origin_state="Kerala", departure_options=["04:00"])
    resp = asyncio.run(optimizer.optimise(req))
    names = [r.market_name for r in resp.recommendations]
    assert set(names) == {"Aluva", "Thrissur"}            # Ludhiana is out of range
    profits = [r.net_profit for r in resp.recommendations]
    assert profits == sorted(profits, reverse=True)
    assert resp.best_market_id == resp.recommendations[0].market_id
    assert all(r.price_source_date != "sample" for r in resp.recommendations)
