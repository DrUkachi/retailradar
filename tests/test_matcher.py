"""
Unit tests for ProductMatcher
Tests fuzzy matching, variant extraction, and normalisation.
"""

import pytest
from src.matcher import ProductMatcher

matcher = ProductMatcher()


class TestFuzzyMatching:
    def test_exact_match_scores_high(self):
        score = matcher._fuzzy_score("Sony WH-1000XM5", "Sony WH-1000XM5")
        assert score >= 85

    def test_padded_title_still_matches(self):
        query   = "Sony WH-1000XM5"
        retail  = "Sony WH-1000XM5 Industry Leading Noise Canceling Wireless Headphones Black"
        score   = matcher._fuzzy_score(query, retail)
        assert score >= 65

    def test_different_product_scores_low(self):
        score = matcher._fuzzy_score("Sony WH-1000XM5", "Apple AirPods Pro 2nd Generation")
        assert score < 65

    def test_walmart_title_format(self):
        query  = "Sony WH-1000XM5"
        retail = "Sony WH1000XM5/B Wireless Noise-Canceling Over-Ear Headphones (Black)"
        score  = matcher._fuzzy_score(query, retail)
        assert score >= 65

    def test_target_title_format(self):
        query  = "Sony WH-1000XM5"
        retail = "Sony Noise Canceling Wireless Headphones WH1000XM5 - Black"
        score  = matcher._fuzzy_score(query, retail)
        assert score >= 65


class TestVariantExtraction:
    def test_storage_extraction(self):
        v = matcher._extract_variants("iPhone 15 Pro 256GB")
        assert v.get("storage") == "256gb"

    def test_color_extraction(self):
        v = matcher._extract_variants("Samsung Galaxy S24 Black")
        assert v.get("color") == "black"

    def test_year_extraction(self):
        v = matcher._extract_variants("MacBook Pro 2023")
        assert v.get("year") == "2023"

    def test_no_variants(self):
        v = matcher._extract_variants("Sony WH-1000XM5")
        assert v == {}

    def test_mismatch_detected(self):
        user_variants     = {"storage": "256gb"}
        retailer_variants = {"storage": "128gb"}
        warning = matcher._variant_mismatch(user_variants, retailer_variants)
        assert warning is not None
        assert "storage" in warning

    def test_no_mismatch_when_same(self):
        user_variants     = {"storage": "256gb"}
        retailer_variants = {"storage": "256gb"}
        warning = matcher._variant_mismatch(user_variants, retailer_variants)
        assert warning is None


class TestScoreRetailerResult:
    def test_exception_returns_not_found(self):
        result = matcher.score_retailer_result(
            Exception("timeout"), "Sony WH-1000XM5", "Sony"
        )
        assert result["confidence"] == "NOT_FOUND"
        assert result["price"] is None

    def test_none_returns_not_found(self):
        result = matcher.score_retailer_result(None, "Sony WH-1000XM5", "Sony")
        assert result["confidence"] == "NOT_FOUND"

    def test_error_dict_returns_not_found(self):
        result = matcher.score_retailer_result(
            {"error": "API key missing"}, "Sony WH-1000XM5", "Sony"
        )
        assert result["confidence"] == "NOT_FOUND"

    def test_good_match_returns_high_confidence(self):
        raw = {
            "title":    "Sony WH-1000XM5 Wireless Headphones",
            "price":    279.99,
            "in_stock": True,
            "url":      "https://amazon.com/dp/XXXX",
        }
        result = matcher.score_retailer_result(raw, "Sony WH-1000XM5", "Sony")
        assert result["confidence"] in ("HIGH", "MEDIUM")
        assert result["price"] == 279.99

    def test_poor_match_returns_low_confidence(self):
        raw = {
            "title":    "Apple AirPods Pro 2nd Generation",
            "price":    249.99,
            "in_stock": True,
            "url":      "https://amazon.com/dp/YYYY",
        }
        result = matcher.score_retailer_result(raw, "Sony WH-1000XM5", "Sony")
        assert result["confidence"] == "LOW"


class TestModelExtraction:
    def test_extracts_model_number(self):
        model = matcher._extract_model("Sony WH-1000XM5 Headphones")
        assert "1000XM5" in model.upper() or "WH" in model.upper()

    def test_no_model_returns_empty(self):
        model = matcher._extract_model("wireless headphones")
        assert model == ""
