"""
Unit tests for PriceCache
"""

import os
import json
import time
import tempfile
import pytest
from unittest.mock import patch

# Redirect cache to a temp file during tests
TMP_CACHE = tempfile.mktemp(suffix=".json")

with patch("src.cache.CACHE_FILE", TMP_CACHE):
    from src.cache import PriceCache


class TestPriceCache:
    def setup_method(self):
        """Fresh cache for each test."""
        if os.path.exists(TMP_CACHE):
            os.remove(TMP_CACHE)
        self.cache = PriceCache()
        self.cache._data = {}

    def teardown_method(self):
        if os.path.exists(TMP_CACHE):
            os.remove(TMP_CACHE)

    def test_rolling_min_none_when_empty(self):
        assert self.cache.get_rolling_min("Sony WH-1000XM5") is None

    def test_rolling_min_after_update(self):
        self.cache.update("Sony WH-1000XM5", {"amazon": 279.99, "walmart": 299.99})
        minimum = self.cache.get_rolling_min("Sony WH-1000XM5")
        assert minimum == 279.99

    def test_rolling_min_tracks_historical_low(self):
        self.cache.update("Sony WH-1000XM5", {"amazon": 279.99})
        self.cache.update("Sony WH-1000XM5", {"amazon": 249.99})  # new low
        self.cache.update("Sony WH-1000XM5", {"amazon": 299.99})  # price rises
        minimum = self.cache.get_rolling_min("Sony WH-1000XM5")
        assert minimum == 249.99

    def test_zero_price_ignored(self):
        self.cache.update("Test Product", {"amazon": 0, "walmart": 59.99})
        minimum = self.cache.get_rolling_min("Test Product")
        assert minimum == 59.99

    def test_key_normalisation(self):
        self.cache.update("Sony WH-1000XM5", {"amazon": 279.99})
        # Different casing should resolve to same key
        m1 = self.cache.get_rolling_min("Sony WH-1000XM5")
        m2 = self.cache.get_rolling_min("sony wh-1000xm5")
        assert m1 == m2

    def test_history_returns_entries(self):
        self.cache.update("Sony WH-1000XM5", {"amazon": 279.99, "walmart": 289.99})
        history = self.cache.get_history("Sony WH-1000XM5")
        assert len(history) == 2
        retailers = {e["retailer"] for e in history}
        assert "amazon" in retailers
        assert "walmart" in retailers
