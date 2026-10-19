"""AgroVessel API -- helps a farmer pick the most profitable mandi to sell a
harvested lot at, weighing distance, live mandi price, fuel cost, and
predicted in-transit spoilage across a few candidate departure times."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.engine.optimizer import CROP_PERISHABILITY, MARKETS, optimise
from app.services.geo import STATES, nearest_state
from app.schemas import RecommendRequest, RecommendResponse

app = FastAPI(title="AgroVessel", description="Mandi routing & spoilage-aware sale recommendations for farmers")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


_PUBLIC_DEMO_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571"


@app.get("/api/price-check")
async def price_check() -> dict:
    """Say whether live mandi prices are working (never reveals the key)."""
    from app.services import mandi_prices

    prices = await mandi_prices.get_latest_prices("Tomato")
    return {
        "api_key_set": settings.data_gov_in_api_key != _PUBLIC_DEMO_KEY,
        "live_prices_working": bool(prices),
        "records_found": len(prices),
        "error": mandi_prices.LAST_ERROR,
    }


@app.get("/api/status")
async def status() -> dict:
    """Health of every external data source (never reveals the key)."""
    import httpx

    from app.services import mandi_prices, weather
    from app.services.routing import get_routes

    prices = await mandi_prices.get_latest_prices("Tomato")
    async with httpx.AsyncClient() as client:
        temps = await weather.get_hourly_forecasts(client, [(13.34, 78.21), (13.03, 77.55)])
        routes = await get_routes(client, 13.34, 78.21, [(13.03, 77.55)])
    return {
        "prices": {"api_key_set": settings.data_gov_in_api_key != _PUBLIC_DEMO_KEY,
                   "working": bool(prices), "tomato_mandis_today": len(prices),
                   "error": mandi_prices.LAST_ERROR},
        "weather": {"working": all(temps), "source": weather.LAST_SOURCE,
                    "open_meteo_error": weather.LAST_ERROR},
        "routing": {"kolar_to_bengaluru_km": round(routes[0][0], 1) if routes else None},
    }


@app.get("/api/states")
async def get_states() -> list[str]:
    return STATES


@app.get("/api/locate")
async def locate(lat: float, lon: float) -> dict:
    """Which state a GPS point is in (nearest known town), for the form."""
    return {"state": nearest_state(lat, lon)}


@app.post("/api/recommend", response_model=RecommendResponse)
async def post_recommend(req: RecommendRequest) -> RecommendResponse:
    return await optimise(req)


@app.get("/api/markets")
async def get_markets() -> list[dict]:
    return MARKETS


@app.get("/api/crops")
async def get_crops() -> list[str]:
    return sorted(CROP_PERISHABILITY.keys())


app.mount("/", StaticFiles(directory="static", html=True), name="static")
