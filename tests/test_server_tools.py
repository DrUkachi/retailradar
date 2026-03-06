"""
Tests for all four server tool handlers.
Tests the handler functions directly without needing live API calls
by mocking the retailer fetchers and matcher.resolve.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

# ── Shared mock data ──────────────────────────────────────────────────────────

MOCK_RESOLVED = {
    "title": "Sony WH-1000XM5 Wireless Headphones",
    "upc":   "027242918368",
    "brand": "Sony",
    "model": "WH-1000XM5",
}

AMAZON_RAW = {
    "title":    "Sony WH-1000XM5 Wireless Headphones",
    "price":    279.99,
    "in_stock": True,
    "url":      "https://amazon.com/dp/B09XS7JWHH",
    "currency": "USD",
}

WALMART_RAW = {
    "title":    "Sony WH-1000XM5 Wireless Noise Canceling Headphones",
    "price":    289.00,
    "in_stock": True,
    "url":      "https://walmart.com/ip/123",
    "currency": "USD",
}

TARGET_RAW = {
    "title":    "Sony Noise Canceling Headphones WH1000XM5",
    "price":    299.99,
    "in_stock": True,
    "url":      "https://target.com/p/456",
    "currency": "USD",
}

# ── Import handlers after patching ───────────────────────────────────────────

from server import (
    handle_compare_prices,
    handle_price_history,
    handle_deal_score,
    handle_availability,
    cache,
)


# ── compare_prices tests ──────────────────────────────────────────────────────

class TestHandleComparePrices:

    @pytest.mark.asyncio
    async def test_returns_all_required_fields(self):
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=TARGET_RAW):

            result = await handle_compare_prices({"product": "Sony WH-1000XM5"})

        assert "product_name"      in result
        assert "match_confidence"  in result
        assert "amazon"            in result
        assert "walmart"           in result
        assert "target"            in result
        assert "cheapest_retailer" in result
        assert "cheapest_price"    in result
        assert "deal_score"        in result
        assert "verdict"           in result
        assert "disclaimer"        in result

    @pytest.mark.asyncio
    async def test_cheapest_retailer_is_amazon(self):
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=TARGET_RAW):

            result = await handle_compare_prices({"product": "Sony WH-1000XM5"})

        assert result["cheapest_retailer"] == "amazon"
        assert result["cheapest_price"]    == 279.99

    @pytest.mark.asyncio
    async def test_price_spread_calculated(self):
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=TARGET_RAW):

            result = await handle_compare_prices({"product": "Sony WH-1000XM5"})

        assert result["price_spread_pct"] is not None
        assert result["price_spread_pct"] > 0

    @pytest.mark.asyncio
    async def test_deal_score_in_valid_range(self):
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=TARGET_RAW):

            result = await handle_compare_prices({"product": "Sony WH-1000XM5"})

        assert 0 <= result["deal_score"] <= 10

    @pytest.mark.asyncio
    async def test_all_retailers_fail_gracefully(self):
        error = Exception("API timeout")
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=error), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=error), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=error):

            result = await handle_compare_prices({"product": "Sony WH-1000XM5"})

        # Should still return a structured result, not crash
        assert "deal_score"  in result
        assert "verdict"     in result
        assert result["cheapest_retailer"] is None

    @pytest.mark.asyncio
    async def test_empty_product_returns_error(self):
        result = await handle_compare_prices({"product": ""})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_product_key_returns_error(self):
        result = await handle_compare_prices({})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_one_retailer_out_of_stock(self):
        target_oos = {**TARGET_RAW, "in_stock": False}
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=target_oos):

            result = await handle_compare_prices({"product": "Sony WH-1000XM5"})

        assert result["target"]["in_stock"] is False
        assert result["cheapest_retailer"] in ("amazon", "walmart")

    @pytest.mark.asyncio
    async def test_upc_query_passes_through(self):
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=TARGET_RAW):

            result = await handle_compare_prices({"product": "027242918368"})

        assert result["upc"] == "027242918368"

    @pytest.mark.asyncio
    async def test_disclaimer_always_present(self):
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=TARGET_RAW):

            result = await handle_compare_prices({"product": "Sony WH-1000XM5"})

        assert "disclaimer" in result
        assert len(result["disclaimer"]) > 10


# ── get_price_history tests ───────────────────────────────────────────────────

class TestHandlePriceHistory:

    @pytest.mark.asyncio
    async def test_no_history_returns_message(self):
        result = await handle_price_history({"product": "totally unknown product xyz"})
        assert result["data_points"] == 0
        assert "No price history" in result["message"]

    @pytest.mark.asyncio
    async def test_history_after_cache_update(self):
        cache.update("Test Headphones v2", {"amazon": 199.99, "walmart": 209.99})
        result = await handle_price_history({"product": "Test Headphones v2"})
        assert result["data_points"] >= 2
        assert result["observed_low"] == 199.99

    @pytest.mark.asyncio
    async def test_empty_product_returns_error(self):
        result = await handle_price_history({"product": ""})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_history_capped_at_50_entries(self):
        # Simulate many cache entries
        for i in range(60):
            cache.update("Popular Product", {"amazon": 100.0 + i})
        result = await handle_price_history({"product": "Popular Product"})
        assert len(result["history"]) <= 50

    @pytest.mark.asyncio
    async def test_returns_required_fields(self):
        result = await handle_price_history({"product": "any product"})
        assert "product"      in result
        assert "data_points"  in result
        assert "history"      in result
        assert "message"      in result


# ── get_deal_score tests ──────────────────────────────────────────────────────

class TestHandleDealScore:

    @pytest.mark.asyncio
    async def test_returns_score_and_verdict(self):
        result = await handle_deal_score({
            "product": "Sony WH-1000XM5",
            "current_price": 279.99,
        })
        assert "deal_score"    in result
        assert "verdict"       in result
        assert "current_price" in result

    @pytest.mark.asyncio
    async def test_score_in_valid_range(self):
        result = await handle_deal_score({
            "product": "AirPods Pro",
            "current_price": 249.99,
        })
        assert 0 <= result["deal_score"] <= 10

    @pytest.mark.asyncio
    async def test_missing_price_returns_error(self):
        result = await handle_deal_score({"product": "Sony WH-1000XM5"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_product_returns_error(self):
        result = await handle_deal_score({"current_price": 199.99})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_high_price_vs_history_scores_low(self):
        cache.update("Budget Speaker", {"amazon": 49.99})
        result = await handle_deal_score({
            "product": "Budget Speaker",
            "current_price": 149.99,   # way above observed low of 49.99
        })
        assert result["deal_score"] <= 4

    @pytest.mark.asyncio
    async def test_price_at_historical_low_scores_high(self):
        cache.update("Gaming Mouse", {"amazon": 39.99})
        result = await handle_deal_score({
            "product": "Gaming Mouse",
            "current_price": 39.99,    # exactly at observed low
        })
        assert result["deal_score"] >= 7


# ── check_availability tests ──────────────────────────────────────────────────

class TestHandleAvailability:

    @pytest.mark.asyncio
    async def test_all_in_stock_returns_correct_summary(self):
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=TARGET_RAW):

            result = await handle_availability({"product": "Sony WH-1000XM5"})

        assert "Available at all three" in result["summary"]

    @pytest.mark.asyncio
    async def test_one_out_of_stock_in_summary(self):
        target_oos = {**TARGET_RAW, "in_stock": False}
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=target_oos):

            result = await handle_availability({"product": "Sony WH-1000XM5"})

        assert "❌ Out of Stock" in result["summary"] or "2 of 3" in result["summary"]

    @pytest.mark.asyncio
    async def test_all_fail_returns_not_confirmed(self):
        error = Exception("timeout")
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=error), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=error), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=error):

            result = await handle_availability({"product": "Sony WH-1000XM5"})

        assert "Not confirmed" in result["summary"]

    @pytest.mark.asyncio
    async def test_returns_urls(self):
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=TARGET_RAW):

            result = await handle_availability({"product": "Sony WH-1000XM5"})

        assert result["amazon"]["url"]  is not None
        assert result["walmart"]["url"] is not None
        assert result["target"]["url"]  is not None

    @pytest.mark.asyncio
    async def test_empty_product_returns_error(self):
        result = await handle_availability({"product": ""})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_product_name_in_result(self):
        with patch("server.matcher.resolve", new_callable=AsyncMock, return_value=MOCK_RESOLVED), \
             patch("server.fetch_amazon",  new_callable=AsyncMock, return_value=AMAZON_RAW), \
             patch("server.fetch_walmart", new_callable=AsyncMock, return_value=WALMART_RAW), \
             patch("server.fetch_target",  new_callable=AsyncMock, return_value=TARGET_RAW):

            result = await handle_availability({"product": "Sony WH-1000XM5"})

        assert result["product_name"] == MOCK_RESOLVED["title"]
