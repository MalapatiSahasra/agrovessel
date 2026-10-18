"""Diesel price lookup.

There is no free, live, public API for Indian retail diesel prices (state oil
marketing companies only publish these through their own apps/websites, which
change daily per-district). Rather than fake a live feed, this module returns a
manually-maintained, approximate per-state table. Treat these as indicative
defaults only -- always let the caller override with today's actual pump price
for a trustworthy result (the frontend exposes this as an editable field).
"""

from app.config import settings

# Rs. per litre. Entries marked with a date were checked against that day's
# capital-city pump price; the rest are older estimates and likely a few
# rupees low. Update periodically.
STATE_DIESEL_PRICE = {
    "karnataka": 98.78,  # capital-city price, 15 Oct 2026
    "andhra pradesh": 97.0,
    "telangana": 103.82,  # capital-city price, 15 Oct 2026
    "tamil nadu": 99.55,  # capital-city price, 15 Oct 2026
    "maharashtra": 97.83,  # capital-city price, 15 Oct 2026
    "gujarat": 92.0,
    "madhya pradesh": 90.0,
    "delhi": 95.20,  # capital-city price, 15 Oct 2026
    "rajasthan": 94.0,
    "uttar pradesh": 91.0,
    "bihar": 95.0,
    "west bengal": 92.0,
    "odisha": 93.0,
    "jharkhand": 95.0,
    "punjab": 90.0,
    "chandigarh": 85.0,
    "assam": 90.0,
}


def get_diesel_price(state: str | None) -> tuple[float, str]:
    """Returns (price_per_litre, source_description)."""
    if state:
        price = STATE_DIESEL_PRICE.get(state.strip().lower())
        if price is not None:
            return price, f"indicative average for {state.strip().title()} (not live)"
    return settings.default_diesel_price, "app default (not live, no state match)"
