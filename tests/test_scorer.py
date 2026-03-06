"""
Unit tests for DealScorer
"""

import pytest
from src.scorer import DealScorer

scorer = DealScorer()


class TestDealScore:
    def test_empty_prices_returns_zero(self):
        assert scorer.compute_deal_score({}, None) == 0

    def test_at_historical_low_scores_high(self):
        prices = {"amazon": 249.99}
        score  = scorer.compute_deal_score(prices, rolling_min=249.99)
        assert score >= 7

    def test_well_above_historical_low_scores_low(self):
        prices = {"amazon": 399.99}
        score  = scorer.compute_deal_score(prices, rolling_min=249.99)
        assert score <= 5

    def test_multiple_retailers_boosts_score(self):
        prices_one   = {"amazon": 279.99}
        prices_three = {"amazon": 279.99, "walmart": 289.99, "target": 299.99}
        score_one    = scorer.compute_deal_score(prices_one,   rolling_min=270.0)
        score_three  = scorer.compute_deal_score(prices_three, rolling_min=270.0)
        assert score_three >= score_one

    def test_large_spread_boosts_score(self):
        prices_tight = {"amazon": 279.99, "walmart": 281.00}
        prices_wide  = {"amazon": 249.99, "walmart": 299.99}
        score_tight  = scorer.compute_deal_score(prices_tight, rolling_min=250.0)
        score_wide   = scorer.compute_deal_score(prices_wide,  rolling_min=250.0)
        assert score_wide >= score_tight

    def test_score_never_exceeds_10(self):
        prices = {"amazon": 249.99, "walmart": 299.99, "target": 319.99}
        score  = scorer.compute_deal_score(prices, rolling_min=249.99)
        assert score <= 10

    def test_no_history_gives_neutral(self):
        prices = {"amazon": 279.99}
        score  = scorer.compute_deal_score(prices, rolling_min=None)
        assert 0 <= score <= 10


class TestVerdictGeneration:
    def test_no_prices_returns_error_message(self):
        verdict = scorer.generate_verdict(
            valid_prices={},
            cheapest_retailer=None,
            cheapest_price=None,
            price_spread_pct=None,
            deal_score=0,
            rolling_min=None,
        )
        assert "Could not retrieve" in verdict

    def test_verdict_mentions_cheapest_retailer(self):
        verdict = scorer.generate_verdict(
            valid_prices={"amazon": 279.99, "walmart": 299.99},
            cheapest_retailer="amazon",
            cheapest_price=279.99,
            price_spread_pct=6.7,
            deal_score=7,
            rolling_min=260.0,
        )
        assert "Amazon" in verdict
        assert "279.99" in verdict

    def test_high_deal_score_recommends_buy(self):
        verdict = scorer.generate_verdict(
            valid_prices={"amazon": 249.99},
            cheapest_retailer="amazon",
            cheapest_price=249.99,
            price_spread_pct=None,
            deal_score=9,
            rolling_min=249.99,
        )
        assert "Buy now" in verdict or "strong deal" in verdict

    def test_low_deal_score_recommends_wait(self):
        verdict = scorer.generate_verdict(
            valid_prices={"amazon": 399.99},
            cheapest_retailer="amazon",
            cheapest_price=399.99,
            price_spread_pct=None,
            deal_score=2,
            rolling_min=249.99,
        )
        assert "wait" in verdict.lower() or "elevated" in verdict.lower()

    def test_no_history_mentions_data_building(self):
        verdict = scorer.generate_verdict(
            valid_prices={"amazon": 279.99},
            cheapest_retailer="amazon",
            cheapest_price=279.99,
            price_spread_pct=None,
            deal_score=4,
            rolling_min=None,
        )
        assert "historical" in verdict.lower() or "history" in verdict.lower()
