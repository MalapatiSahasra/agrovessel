import json
import random

# Load crops and markets
with open('app/data/crop_perishability.json', 'r') as f:
    crops = json.load(f)

with open('app/data/markets.json', 'r') as f:
    markets = json.load(f)

# Rough average wholesale prices per kg in INR
base_prices = {
    "tomato": 20, "onion": 25, "potato": 15, "brinjal": 30, "cabbage": 12,
    "cauliflower": 18, "okra": 40, "green chilli": 45, "carrot": 35, "cucumber": 20,
    "banana": 30, "mango": 80, "grapes": 70, "papaya": 25, "spinach": 15,
    "coriander": 50, "beans": 40, "peas": 60, "garlic": 120, "ginger": 90,
    "watermelon": 15, "pomegranate": 100
}

sample_prices = {}

for crop_name in crops.keys():
    crop_lower = crop_name.lower()
    base = base_prices.get(crop_lower, 30) # default to 30 if not in dictionary
    
    sample_prices[crop_lower] = {}
    
    for m in markets:
        # Add random variation (-20% to +30%) to simulate different market rates
        variation = random.uniform(0.8, 1.3)
        price = round(base * variation, 1)
        
        sample_prices[crop_lower][m["id"]] = {
            "price_per_kg": price,
            "market": m["name"],
            "district": m["district"],
            "state": m["state"]
        }

with open('app/data/sample_prices.json', 'w') as f:
    json.dump(sample_prices, f, indent=2)

print("Generated sample prices for", len(sample_prices), "crops across", len(markets), "markets.")
