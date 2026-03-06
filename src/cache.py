"""
Price Cache
Lightweight rolling price minimum tracker using a local JSON file.
Records observed prices per product to build a deal signal over time.
This replaces the need for Keepa's expensive API for historical lows in v1.
"""

import json
import os
import time
from typing import Optional

CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "cache", "price_history.json")
MAX_ENTRIES_PER_PRODUCT = 500   # keep last 500 price observations per product
CACHE_TTL_DAYS = 180            # ignore entries older than 180 days


class PriceCache:
    """
    Stores { product_name: [ {ts, retailer, price}, ... ] }
    Computes rolling minimum from recent observations.
    """

    def __init__(self):
        self._data: dict = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, "r") as f:
                    self._data = json.load(f)
        except (json.JSONDecodeError, IOError):
            self._data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump(self._data, f, indent=2)
        except IOError:
            pass  # non-critical: cache write failure doesn't break the tool

    def _key(self, product_name: str) -> str:
        """Normalise product name to a stable cache key."""
        return product_name.lower().strip()

    def update(self, product_name: str, prices: dict[str, float]):
        """Record current prices for a product."""
        key = self._key(product_name)
        if key not in self._data:
            self._data[key] = []

        now = int(time.time())
        for retailer, price in prices.items():
            if price and price > 0:
                self._data[key].append({
                    "ts":       now,
                    "retailer": retailer,
                    "price":    price,
                })

        # Trim old entries
        cutoff = now - (CACHE_TTL_DAYS * 86400)
        self._data[key] = [
            e for e in self._data[key] if e["ts"] >= cutoff
        ][-MAX_ENTRIES_PER_PRODUCT:]

        self._save()

    def get_rolling_min(self, product_name: str) -> Optional[float]:
        """Return the lowest price ever observed for this product (within TTL)."""
        key = self._key(product_name)
        entries = self._data.get(key, [])
        if not entries:
            return None
        prices = [e["price"] for e in entries if e.get("price", 0) > 0]
        return min(prices) if prices else None

    def get_history(self, product_name: str) -> list[dict]:
        """Return all price history entries for a product (for debugging)."""
        return self._data.get(self._key(product_name), [])
