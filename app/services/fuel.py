"""Diesel price lookup.

There is no free, live, public API for Indian retail diesel prices (state oil
marketing companies only publish these through their own apps/websites, which
change daily per-district). Rather than fake a live feed, this module returns a
manually-maintained, approximate per-state table. Treat these as indicative
defaults only -- always let the caller override with today's actual pump price
for a trustworthy result (the frontend exposes this as an editable field).
"""

from app.config import settings
from app.services.geo import canonical_state

# Rs. per litre, state-level diesel price as published on 17 Oct 2026
# (Goodreturns state table; Andhra Pradesh from Business Today, 18 Oct 2026).
# Pump prices change; update this table periodically. The farmer can always
# override it with today's price in the form.
STATE_DIESEL_PRICE = {
    "andaman and nicobar": 84.56,
    "andhra pradesh": 105.61,
    "arunachal pradesh": 87.32,
    "assam": 98.07,
    "bihar": 99.36,
    "chandigarh": 89.47,
    "chhattisgarh": 101.55,
    "dadra and nagar haveli and daman and diu": 95.33,
    "delhi": 95.20,
    "goa": 95.88,
    "gujarat": 98.13,
    "haryana": 96.01,
    "himachal pradesh": 95.28,
    "jammu and kashmir": 92.81,
    "jharkhand": 101.31,
    "karnataka": 99.56,
    "kerala": 104.40,
    "ladakh": 94.73,
    "lakshadweep": 102.80,
    "madhya pradesh": 99.64,
    "maharashtra": 97.83,
    "manipur": 92.59,
    "meghalaya": 94.13,
    "mizoram": 95.16,
    "nagaland": 96.18,
    "odisha": 100.68,
    "puducherry": 93.79,
    "punjab": 95.47,
    "rajasthan": 98.25,
    "sikkim": 97.70,
    "tamil nadu": 99.55,
    "telangana": 104.23,
    "tripura": 93.85,
    "uttar pradesh": 95.36,
    "uttarakhand": 95.05,
    "west bengal": 99.82,
}


def get_diesel_price(state: str | None) -> tuple[float, str]:
    """Returns (price_per_litre, source_description)."""
    if state:
        canon = canonical_state(state) or state.strip()
        price = STATE_DIESEL_PRICE.get(canon.lower())
        if price is not None:
            return price, f"{canon} state price, 17 Oct 2026 (not live)"
    return settings.default_diesel_price, "app default (not live, no state match)"
