"""Synthetic labelled narrations for the ML fallback categoriser (SPEC §5.4).

Local merchants are composed from a prefix + a category-bearing suffix ("Shree Sai" + "Medical").
Two disjoint prefix pools exist: TRAIN_PREFIXES for training/holdout and PERSONA_PREFIXES for the
demo personas' local merchants, so the demo never categorises a merchant the model trained on.
Caveat (D15): template narrations are cleaner than real bank data; reported accuracy is optimistic.
"""
from __future__ import annotations

import numpy as np

TRAIN_PREFIXES = [
    "Shree", "Sai", "Om", "New", "Jai", "Balaji", "Ganesh", "Laxmi", "Royal", "City", "Anand", "Krishna",
    "Mahalaxmi", "Shiv", "Maruti", "Hanuman", "Gurukrupa", "Siddhi", "Vinayak", "Swami", "Sri Ram", "Radhe",
    "Ambika", "Bharat", "National", "Modern", "Classic", "Star", "Sun", "Moon", "Golden", "Silver", "Apna",
    "Hind", "Janata", "Pioneer", "Sagar", "Sahyadri", "Kaveri", "Ganga", "Yamuna", "Narmada", "Tirupati",
]
PERSONA_PREFIXES = [
    "Vaishali", "Durga", "Kalpana", "Pune", "Deccan", "Shivaji", "Kothrud", "Aundh", "Baner", "Indiranagar",
    "Koramangala", "Karol Bagh", "Lajpat", "Saket", "Dwarka", "Chandni", "Kamla", "Rohini",
]

SUFFIXES: dict[str, list[str]] = {
    "groceries": ["Kirana Stores", "General Stores", "Supermarket", "Provision Store", "Mart", "Fresh Mart",
                  "Vegetables", "Dairy", "Kirana", "Grocery", "Fruits and Vegetables", "Bhandar", "Super Bazaar"],
    "health": ["Medical", "Medicos", "Pharmacy", "Chemist", "Clinic", "Diagnostics", "Hospital", "Dental Care",
               "Path Lab", "Medical and General Stores", "Nursing Home", "Eye Care"],
    "fuel": ["Petroleum", "Fuels", "Petrol Pump", "Service Station", "Filling Station", "Fuel Point",
             "Auto Fuels", "Petro Services"],
    "dining": ["Restaurant", "Cafe", "Dhaba", "Bhojanalaya", "Veg Restaurant", "Snacks Centre", "Sweets",
               "Bakery", "Biryani House", "Tea Stall", "Family Restaurant", "Food Court", "Chinese Corner"],
    "transport": ["Cabs", "Auto Services", "Taxi Services", "Parking", "Toll Plaza", "Bike Rentals"],
    "travel": ["Holidays", "Tours and Travels", "Air Travels", "Hotels and Resorts", "Lodge", "Guest House",
               "Resort", "Yatra Services"],
    "shopping": ["Garments", "Fashion", "Footwear", "Electronics", "Mobile Shop", "Collection", "Textiles",
                 "Novelty", "Gift Centre", "Jewellers", "Opticals", "Hardware", "Furniture", "Home Decor"],
    "utilities": ["Gas Agency", "Water Supply", "Power Services", "Electricals", "Plumbing Services"],
    "telecom": ["Telecom", "Mobile Recharge", "Communications", "Broadband", "Cable Network", "Net Services"],
    "education": ["Academy", "Classes", "Coaching Centre", "School", "Institute", "Tuition", "Library",
                  "Tutorials", "Book Depot", "Stationers"],
    "insurance": ["Insurance", "Insurance Brokers", "Assurance", "Insurance Services"],
    "entertainment": ["Cinemas", "Multiplex", "Gaming Zone", "Bowling", "Events", "Theatre", "Fun World",
                      "Club"],
}
CLASSES = sorted(SUFFIXES)

AMOUNT_RANGES = {  # rupees (lo, hi), log-uniform
    "groceries": (80, 3500), "health": (60, 5000), "fuel": (200, 4000), "dining": (60, 2500),
    "transport": (30, 1500), "travel": (800, 25000), "shopping": (150, 15000), "utilities": (200, 3500),
    "telecom": (149, 1500), "education": (300, 30000), "insurance": (500, 30000), "entertainment": (150, 3000),
}
CITIES = ["PUNE", "MUMBAI", "BENGALURU", "DELHI", "GURUGRAM", "NOIDA", "HYDERABAD", "CHENNAI", "NASHIK"]
BANKS = ["YESB", "ICIC", "HDFC", "SBIN", "UTIB", "KKBK", "PYTM", "AIRP"]
ABBREV = {"Medical": "Med", "General": "Gen", "Stores": "Strs", "Restaurant": "Rest", "Services": "Svcs",
          "Centre": "Ctr", "Supermarket": "Supmkt", "Petroleum": "Petro", "Electronics": "Elec"}


def merchant_names(prefixes: list[str]) -> list[tuple[str, str]]:
    """All (name, category) combinations for a prefix pool."""
    return [(f"{p} {s}", cat) for cat in CLASSES for s in SUFFIXES[cat] for p in prefixes]


def _noisy(name: str, rng: np.random.Generator) -> str:
    words = name.split()
    if rng.random() < 0.25:
        words = [ABBREV.get(w, w) for w in words]
    s = " ".join(words).upper()
    if rng.random() < 0.3:
        s = s[: int(rng.integers(12, 22))].rstrip()  # banks truncate payee names
    return s


def narration_for(name: str, rng: np.random.Generator) -> tuple[str, str]:
    """(narration, channel) in one of the common formats."""
    payee = _noisy(name, rng)
    r = rng.random()
    if r < 0.55:
        vpa = payee.lower().replace(" ", "")[:14] + ("@ybl" if rng.random() < 0.5 else "@paytm")
        return (f"UPI/DR/{rng.integers(10**11, 10**12)}/{payee}/{rng.choice(BANKS)}/{vpa}/"
                f"{rng.choice(['Payment', 'UPI', 'Pay to merchant', ''])}"), "upi"
    if r < 0.8:
        return f"POS {rng.integers(4000, 5999)}XXXXXXXX{rng.integers(1000, 9999)} {payee} {rng.choice(CITIES)}", "pos"
    return f"{payee} {rng.choice(CITIES)}", "card"


def sample_amount(cat: str, rng: np.random.Generator) -> int:
    lo, hi = AMOUNT_RANGES[cat]
    return int(round(float(np.exp(rng.uniform(np.log(lo), np.log(hi)))))) * 100


def training_rows(rng: np.random.Generator, per_merchant: int = 3) -> list[dict]:
    rows = []
    for name, cat in merchant_names(TRAIN_PREFIXES):
        for _ in range(per_merchant):
            narration, channel = narration_for(name, rng)
            rows.append({"merchant": name, "narration": narration, "channel": channel,
                         "amount_paise": sample_amount(cat, rng), "category": cat})
    return rows
