"""Quick test: Ravi's tomato scenario."""
import asyncio
import json
from app.schemas import RecommendRequest
from app.engine.optimizer import optimise

async def main():
    req = RecommendRequest(
        crop="Tomato",
        quantity_kg=2000,
        harvest_cost_total=15000,
        origin_lat=13.1367,
        origin_lon=78.1298,
        origin_state="Karnataka",
        local_price_per_kg=5.0,
        truck_mileage_kmpl=12,
        diesel_price_per_litre=91,
        toll_per_km=2.0,
        driver_fee=1500,
        max_distance_km=300,
        departure_options=["04:00", "07:00", "10:00", "13:00", "19:00", "22:00"],
        max_candidate_markets=6,
    )
    result = await optimise(req)
    print(json.dumps(result.model_dump(), indent=2, default=str))

asyncio.run(main())
