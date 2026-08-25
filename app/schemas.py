from pydantic import BaseModel, Field


class RecommendRequest(BaseModel):
    crop: str = Field(..., description="Crop name, e.g. 'Tomato'")
    quantity_kg: float = Field(..., gt=0)
    harvest_cost_total: float | None = Field(
        None, description="Total cost incurred to grow/harvest this lot (Rs), for loss context only"
    )
    origin_lat: float
    origin_lon: float
    origin_state: str = Field(..., description="Farmer's home state, used to scope the mandi price search")
    local_price_per_kg: float | None = Field(
        None, description="Known price at the farmer's own village/local market (Rs/kg), if not in our market list"
    )
    truck_mileage_kmpl: float = Field(12.0, gt=0, description="Truck fuel efficiency, km per litre")
    diesel_price_per_litre: float | None = Field(None, description="Override the default diesel price (Rs/litre)")
    toll_per_km: float = Field(2.0, ge=0, description="Estimated highway toll, Rs per km (one-way)")
    driver_fee: float = Field(1500.0, ge=0, description="Flat driver/labour fee for the trip (Rs)")
    max_distance_km: float = Field(300.0, gt=0, le=600)
    market_closes_hour: int = Field(18, ge=0, le=23, description="Hour (24h) after which a mandi is assumed closed")
    departure_options: list[str] = Field(
        default_factory=lambda: ["04:00", "07:00", "10:00", "13:00"],
        description="Candidate departure times (HH:MM, local) to evaluate",
    )
    max_candidate_markets: int = Field(6, ge=1, le=15)


class DepartureOption(BaseModel):
    departure_time: str
    arrival_time: str
    avg_transit_temp_c: float | None
    predicted_loss_pct: float
    feasible: bool
    reason: str


class MarketRecommendation(BaseModel):
    market_id: str
    market_name: str
    state: str
    is_local: bool
    distance_km: float
    duration_hr: float
    mandi_price_per_kg: float
    price_source_date: str | None
    best_departure: DepartureOption | None
    usable_kg: float
    lost_kg: float
    effective_price_per_kg: float
    gross_revenue: float
    transport_cost: float
    net_profit: float
    notes: list[str]


class RecommendResponse(BaseModel):
    crop: str
    quantity_kg: float
    diesel_price_per_litre: float
    diesel_price_source: str
    recommendations: list[MarketRecommendation]
    best_market_id: str | None
    disclaimers: list[str]
