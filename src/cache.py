"""
Price Cache
Lightweight rolling price minimum tracker using a local JSON file.
Records observed prices per product to build a deal signal over time.
This replaces the need for Keepa's expensive API for historical lows in v1.

Also provides a short-lived in-memory response cache (TTL: 10 minutes) so
repeat queries for the same product are served instantly without hitting
SerpApi/ScraperAPI again. This keeps response times well under the CTX
30-second soft limit for popular products.
"""

import json
import os
import time
from typing import Optional

CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "cache", "price_history.json")
MAX_ENTRIES_PER_PRODUCT = 500   # keep last 500 price observations per product
CACHE_TTL_DAYS = 180            # ignore entries older than 180 days
RESPONSE_CACHE_TTL = 600        # 10 minutes — in-memory API response cache


class PriceCache:
    """
    Stores { product_name: [ {ts, retailer, price}, ... ] }
    Computes rolling minimum from recent observations.
    Also maintains a short-lived in-memory response cache for live API results.
    """

    def __init__(self):
        self._data: dict = {}
        self._response_cache: dict = {}   # { cache_key: (timestamp, result_dict) }
        self._load()

    # ── Response cache (in-memory, TTL 10 min) ────────────────────────────────

    def _response_key(self, product: str, size: str | None, zip_code: str) -> str:
        """Stable cache key for a live API response."""
        parts = [product.lower().strip()]
        if size:
            parts.append(f"sz{size.lower().strip()}")
        parts.append(zip_code)
        return "|".join(parts)

    def get_response(self, product: str, size: str | None, zip_code: str) -> dict | None:
        """Return cached API response if still fresh, else None."""
        key = self._response_key(product, size, zip_code)
        entry = self._response_cache.get(key)
        if entry:
            ts, result = entry
            if time.time() - ts < RESPONSE_CACHE_TTL:
                return result
            del self._response_cache[key]
        return None

    def set_response(self, product: str, size: str | None, zip_code: str, result: dict):
        """Store a live API response in the in-memory cache."""
        key = self._response_key(product, size, zip_code)
        self._response_cache[key] = (time.time(), result)

    # ── Price history (persistent JSON) ───────────────────────────────────────

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
        """Return all price history entries for a product."""
        return self._data.get(self._key(product_name), [])
