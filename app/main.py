"""AgroVessel API -- helps a farmer pick the most profitable mandi to sell a
harvested lot at, weighing distance, live mandi price, fuel cost, and
predicted in-transit spoilage across a few candidate departure times."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.engine.optimizer import CROP_PERISHABILITY, MARKETS, optimise
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
