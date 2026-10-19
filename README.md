# AgroVessel

**Which mandi should a farmer truck this harvest to today?**

AgroVessel is a web decision-support tool for Indian farmers. For a harvested lot it
ranks every wholesale market (mandi) within driving range by **expected net profit**,
weighing four things together:

1. **Live mandi price** – all-India daily modal prices from data.gov.in (Agmarknet).
2. **Transport cost** – real road distance (OSRM), round-trip diesel at the state's
   pump price, tolls and a driver fee.
3. **In-transit spoilage** – a Q10 temperature model driven by the hourly weather
   forecast (Open-Meteo) along the trip, plus a buyer markdown for damaged produce.
4. **Departure time** – each candidate time is checked against market closing time,
   traffic (rush hour vs. night) and the temperature during the trip.

It works for all 28 states and 8 union territories, in English and Hindi.

Live site: https://agrovessel.vercel.app

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then put your own DATA_GOV_IN_API_KEY in .env
uvicorn app.main:app --reload # open http://localhost:8000
python -m pytest -q           # 25 tests, no network needed
```

## Live prices: the API key

The public demo key in `.env.example` is **rejected** by data.gov.in. Without your own
key the app falls back to clearly-labelled *sample* prices that are **not real data**.

1. Sign up free at https://data.gov.in → My Account → API Key.
2. Vercel → project **agrovessel** → Settings → Environment Variables →
   add `DATA_GOV_IN_API_KEY` (tick **Production**), then redeploy.
3. Check https://agrovessel.vercel.app/api/price-check – it should show
   `"api_key_set": true` and `"live_prices_working": true`.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/recommend` | Ranked market recommendations (see `app/schemas.py`) |
| GET | `/api/crops` | Supported crops |
| GET | `/api/states` | All states / UTs |
| GET | `/api/locate?lat=&lon=` | State for a GPS point |
| GET | `/api/markets` | Curated market list (fallback) |
| GET | `/api/price-check` | Is live price data working? |
| GET | `/health` | Liveness |

## Project layout

```
app/main.py                 FastAPI app and routes
app/engine/optimizer.py     Decision engine (spoilage, cost, ranking)
app/services/mandi_prices.py  data.gov.in client (all-India, paged)
app/services/geo.py         Offline gazetteer: state names, mandi locations
app/services/routing.py     OSRM road distance (batched /table call)
app/services/weather.py     Open-Meteo hourly temperature (batched)
app/services/fuel.py        State diesel prices
app/data/                   Crop perishability, curated markets, gazetteer, samples
static/index.html           Web front end (English / Hindi)
tests/                      Unit and end-to-end tests (mocked APIs)
```

## Data credits

- Mandi prices: Government of India, data.gov.in / Agmarknet (Open Government Data License – India).
- Town gazetteer: GeoNames (CC BY 4.0), via the `reverse_geocoder` package data.
- Routing: OSRM / OpenStreetMap contributors (ODbL). Weather: Open-Meteo (CC BY 4.0).
- Diesel prices: state tables published 17–18 Oct 2026; update periodically.

## Limits

Spoilage and markdown parameters are literature-based heuristics, not field-calibrated.
Recommendations are advice only – farmers should confirm prices before travelling.
