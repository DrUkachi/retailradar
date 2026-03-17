"""
Multi-Retailer Price Intelligence Feed
CTX Protocol MCP Server - Python

Tools exposed:
  1. compare_prices     — full cross-retailer price comparison + deal score
  2. get_price_history  — rolling price history for a product from local cache
  3. get_deal_score     — deal score only (fast, lightweight)
  4. check_availability — stock availability across all three retailers
"""

import asyncio
import json
import os
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from src.matcher import ProductMatcher
from src.retailers import fetch_amazon, fetch_walmart, fetch_target, fetch_best_buy
from src.scorer import DealScorer
from src.cache import PriceCache

server  = Server("price-intelligence-feed")
matcher = ProductMatcher()
scorer  = DealScorer()
cache   = PriceCache()

# ── CTX Protocol _meta ────────────────────────────────────────────────────────
# surface: "query"       = shows up in Context chat app
# queryEligible: True    = Context app can call this to answer user queries
# pricing.responseUsd    = what CTX charges per call (you keep 90%)

CTX_META_FULL = {
    "ctx": {
        "surface": "query",
        "queryEligible": True,
        "pricing": {"responseUsd": 0.10}
    }
}

CTX_META_LIGHT = {
    "ctx": {
        "surface": "query",
        "queryEligible": True,
        "pricing": {"responseUsd": 0.05}
    }
}

# ── Schema-compliant error responses ──────────────────────────────────────────
# Every tool must return all required fields from its outputSchema even on error.

def _error_response(tool_name: str, message: str) -> dict:
    """Returns a schema-compliant error dict for any tool."""
    base = {
        "error":   message,
        "verdict": f"An error occurred: {message}",
        "_meta":   CTX_META_LIGHT,
    }
    # Add required fields per tool so outputSchema validation always passes
    if tool_name == "compare_prices":
        base.update({
            "product_name":      "Unknown",
            "match_confidence":  "LOW",
            "deal_score":        0,
            "disclaimer":        "Could not retrieve data. Please try again.",
        })
    elif tool_name == "get_price_history":
        base.update({
            "product":     "Unknown",
            "data_points": 0,
            "history":     [],
            "message":     f"Error: {message}",
        })
    elif tool_name == "get_deal_score":
        base.update({
            "product":       "Unknown",
            "current_price": 0,
            "deal_score":    0,
        })
    elif tool_name == "check_availability":
        base.update({
            "product_name": "Unknown",
            "summary":      f"Error: {message}",
        })
    return base




# ── Tool definitions ──────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [

        # Tool 1: Full price comparison (flagship)
        types.Tool(
            name="compare_prices",
            description=(
                "Compare real-time product prices across Amazon, Walmart, Target, and Best Buy. "
                "Returns current prices from all four retailers, identifies the cheapest option, "
                "calculates a deal score from 0 to 10 based on historical price data, "
                "and gives a plain-English buy-now-or-wait recommendation. "
                "Use this when a user wants to know where to buy a product for the best price today. "
                "Accepts a product name like 'Sony WH-1000XM5', a model number, or a UPC barcode. "
                "SIZE HANDLING: If the user mentions a size (e.g. 'Nike Air Force 1 size 10.5'), "
                "pass the product name WITHOUT the size in the product field, and pass the size "
                "value separately in the size field. Never embed size in the product string. "
                "RETRY POLICY: Call this tool ONCE per product. "
                "If retailers_found is 0 or search_exhausted is true after one call, "
                "DO NOT retry with different query variations — the absence IS the final answer. "
                "Read no_results_advice for what to tell the user. "
                "Only call again if the user explicitly asks about a different product."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "description": (
                            "The product to search for. Can be a product name "
                            "('Sony WH-1000XM5'), model number ('RTX 4090'), "
                            "or UPC barcode ('043396630833')."
                        ),
                    },
                    "size": {
                        "type": "string",
                        "description": (
                            "Clothing or shoe size to filter results by, e.g. '10.5', '11', 'M', 'XL'. "
                            "IMPORTANT: When the user mentions a size (e.g. 'size 10.5', 'size large'), "
                            "extract it and pass it here separately — do NOT include it in the product string. "
                            "Correct: product='Nike Air Force 1 Low', size='10.5'. "
                            "Wrong: product='Nike Air Force 1 Low size 10.5'."
                        ),
                    },
                    "zip_code": {
                        "type": "string",
                        "description": "US ZIP code for localised pricing. Defaults to 10001 (New York).",
                        "default": "10001",
                    },
                },
                "required": ["product"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "product_name":      {"type": "string"},
                    "match_confidence":  {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "retailers_found":   {"type": "integer", "description": "Number of retailers (0-4) with a valid price. All four retailer fields are always present objects — check the 'found' field on each to see if they have a listing."},
                    "search_exhausted":  {"type": "boolean", "description": "STOP SIGNAL: When true, all four retailers were searched and found nothing. DO NOT retry. Present no_results_advice to the user as the final answer."},
                    "no_results_reason": {"type": "string", "description": "Why no results were found. Empty string when results were found. 'no_matching_listings' = not listed at these retailers."},
                    "no_results_advice": {"type": "string", "description": "FINAL ANSWER STRING: When search_exhausted is true, present this text directly to the user. Empty string when results were found. Do not retry."},
                    "amazon":       {
                        "type": "object",
                        "description": "ALWAYS a non-null object. Check found=true/false before reading price/url. If not_listed=true, this retailer does not carry the product — all numeric fields will be 0 and string fields will be empty string. This is NOT a bug.",
                        "properties": {
                            "found":            {"type": "boolean", "description": "true if retailer has a listing, false if not. Always present."},
                            "not_listed":       {"type": "boolean", "description": "true when retailer does not carry this product. false when found=true."},
                            "price":            {"type": "number",  "description": "Listed price in USD. 0 when not_listed=true — this is correct, NOT a missing value."},
                            "effective_price":  {"type": "number",  "description": "Price after coupon. Equals price when no coupon. 0 when not_listed=true."},
                            "in_stock":         {"type": "boolean"},
                            "url":              {"type": "string",  "description": "Product URL. Empty string when not_listed=true."},
                            "title":            {"type": "string",  "description": "Product title. Empty string when not_listed=true."},
                            "confidence":       {"type": "string",  "enum": ["HIGH", "MEDIUM", "LOW", "NOT_FOUND"]},
                            "coupon_available": {"type": "boolean"},
                            "coupon_text":      {"type": "string"},
                            "coupon_discount":  {"type": "number",  "description": "Coupon discount amount in USD. 0 means no coupon — this is the correct default, NOT a missing value. Only non-zero when coupon_available=true."},
                            "size_match":       {"type": "boolean"},
                            "condition":        {"type": "string",  "description": "Product condition: new, renewed, used. Empty string when not_listed=true."}
                        }
                    },
                    "walmart":      {
                        "type": "object",
                        "description": "ALWAYS a non-null object. Check found=true/false before reading price/url. If not_listed=true, this retailer does not carry the product — all numeric fields will be 0 and string fields will be empty string. This is NOT a bug.",
                        "properties": {
                            "found":            {"type": "boolean", "description": "true if retailer has a listing, false if not. Always present."},
                            "not_listed":       {"type": "boolean", "description": "true when retailer does not carry this product. false when found=true."},
                            "price":            {"type": "number",  "description": "Listed price in USD. 0 when not_listed=true — this is correct, NOT a missing value."},
                            "effective_price":  {"type": "number",  "description": "Price after coupon. Equals price when no coupon. 0 when not_listed=true."},
                            "in_stock":         {"type": "boolean"},
                            "url":              {"type": "string",  "description": "Product URL. Empty string when not_listed=true."},
                            "title":            {"type": "string",  "description": "Product title. Empty string when not_listed=true."},
                            "confidence":       {"type": "string",  "enum": ["HIGH", "MEDIUM", "LOW", "NOT_FOUND"]},
                            "coupon_available": {"type": "boolean"},
                            "coupon_text":      {"type": "string"},
                            "coupon_discount":  {"type": "number",  "description": "Coupon discount amount in USD. 0 means no coupon — this is the correct default, NOT a missing value. Only non-zero when coupon_available=true."},
                            "size_match":       {"type": "boolean"},
                            "condition":        {"type": "string",  "description": "Product condition: new, renewed, used. Empty string when not_listed=true."}
                        }
                    },
                    "target":       {
                        "type": "object",
                        "description": "ALWAYS a non-null object. Check found=true/false before reading price/url. If not_listed=true, this retailer does not carry the product — all numeric fields will be 0 and string fields will be empty string. This is NOT a bug.",
                        "properties": {
                            "found":            {"type": "boolean", "description": "true if retailer has a listing, false if not. Always present."},
                            "not_listed":       {"type": "boolean", "description": "true when retailer does not carry this product. false when found=true."},
                            "price":            {"type": "number",  "description": "Listed price in USD. 0 when not_listed=true — this is correct, NOT a missing value."},
                            "effective_price":  {"type": "number",  "description": "Price after coupon. Equals price when no coupon. 0 when not_listed=true."},
                            "in_stock":         {"type": "boolean"},
                            "url":              {"type": "string",  "description": "Product URL. Empty string when not_listed=true."},
                            "title":            {"type": "string",  "description": "Product title. Empty string when not_listed=true."},
                            "confidence":       {"type": "string",  "enum": ["HIGH", "MEDIUM", "LOW", "NOT_FOUND"]},
                            "coupon_available": {"type": "boolean"},
                            "coupon_text":      {"type": "string"},
                            "coupon_discount":  {"type": "number",  "description": "Coupon discount amount in USD. 0 means no coupon — this is the correct default, NOT a missing value. Only non-zero when coupon_available=true."},
                            "size_match":       {"type": "boolean"},
                            "condition":        {"type": "string",  "description": "Product condition: new, renewed, used. Empty string when not_listed=true."}
                        }
                    },
                    "best_buy":     {
                        "type": "object",
                        "description": "ALWAYS a non-null object. Check found=true/false before reading price/url. If not_listed=true, this retailer does not carry the product — all numeric fields will be 0 and string fields will be empty string. This is NOT a bug.",
                        "properties": {
                            "found":            {"type": "boolean", "description": "true if retailer has a listing, false if not. Always present."},
                            "not_listed":       {"type": "boolean", "description": "true when retailer does not carry this product. false when found=true."},
                            "price":            {"type": "number",  "description": "Listed price in USD. 0 when not_listed=true — this is correct, NOT a missing value."},
                            "effective_price":  {"type": "number",  "description": "Price after coupon. Equals price when no coupon. 0 when not_listed=true."},
                            "in_stock":         {"type": "boolean"},
                            "url":              {"type": "string",  "description": "Product URL. Empty string when not_listed=true."},
                            "title":            {"type": "string",  "description": "Product title. Empty string when not_listed=true."},
                            "confidence":       {"type": "string",  "enum": ["HIGH", "MEDIUM", "LOW", "NOT_FOUND"]},
                            "coupon_available": {"type": "boolean"},
                            "coupon_text":      {"type": "string"},
                            "coupon_discount":  {"type": "number",  "description": "Coupon discount amount in USD. 0 means no coupon — this is the correct default, NOT a missing value. Only non-zero when coupon_available=true."},
                            "size_match":       {"type": "boolean"},
                            "condition":        {"type": "string",  "description": "Product condition: new, renewed, used. Empty string when not_listed=true."}
                        }
                    },
                    "cheapest_retailer": {"type": "string", "description": "Name of cheapest retailer. Empty string if no prices found."},
                    "cheapest_price":    {"type": "number", "description": "Cheapest price found. 0 if no prices found."},
                    "price_spread_pct":  {"type": "number", "description": "Percentage difference between highest and lowest price. 0 when fewer than 2 retailers returned prices."},
                    "deal_score":        {"type": "integer", "minimum": 0, "maximum": 10},
                    "verdict":           {"type": "string"},
                    "price_context":     {"type": "string", "description": "Condition warning and market trend note, e.g. if cheapest result is refurbished or prices are at a historic low. Empty string if no context to add."},
                    "disclaimer":        {"type": "string"},
                },
                "required": ["product_name", "match_confidence", "deal_score", "verdict", "price_context", "disclaimer"],
            },
        ),

        # Tool 2: Price history
        types.Tool(
            name="get_price_history",
            description=(
                "Retrieve the observed price history for a product from the local cache. "
                "Returns all previously recorded prices across Amazon, Walmart, Target, and Best Buy, "
                "including the all-time observed low price and how many data points have been collected. "
                "Use this when a user wants to understand how a product's price has changed over time. "
                "Note: history only exists for products previously queried via compare_prices. "
                "RETRY POLICY: Call this tool ONCE. "
                "If data_points is 0, no history exists yet — DO NOT retry with alternate names."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "description": "Product name to look up history for.",
                    },
                },
                "required": ["product"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "product":      {"type": "string"},
                    "observed_low": {"type": "number", "description": "Lowest price ever observed. 0 when no history exists yet — this is NOT an error, it means the product has not been tracked before."},
                    "data_points":  {"type": "integer"},
                    "history":      {"type": "array"},
                    "message":      {"type": "string", "description": "If data_points is 0, this explains why. When data_points is 0, this IS the final answer — do not retry."},
                },
                "required": ["product", "data_points", "history", "message"],
            },
        ),

        # Tool 3: Deal score only (lightweight)
        types.Tool(
            name="get_deal_score",
            description=(
                "Get a quick deal score from 0 to 10 for a product at a given price. "
                "Use this when you already know the current price and just want to know "
                "if it is a good deal compared to historical prices observed by this tool. "
                "Faster and cheaper than compare_prices. "
                "Use it for follow-up questions like 'is $279 a good price for Sony WH-1000XM5?' "
                "Returns the score, the observed historical low, and a short verdict."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "description": "Product name to score.",
                    },
                    "current_price": {
                        "type": "number",
                        "description": "The price to evaluate, e.g. 279.99",
                    },
                },
                "required": ["product", "current_price"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "product":       {"type": "string"},
                    "current_price": {"type": "number"},
                    "deal_score":    {"type": "integer", "minimum": 0, "maximum": 10},
                    "observed_low":  {"type": "number", "description": "Lowest price ever observed. 0 when no history exists yet."},
                    "verdict":       {"type": "string"},
                },
                "required": ["product", "current_price", "deal_score", "verdict"],
            },
        ),

        # Tool 4: Availability check
        types.Tool(
            name="check_availability",
            description=(
                "Check whether a product is currently in stock at Amazon, Walmart, Target, and Best Buy. "
                "Returns in-stock status and a direct URL for each of the four retailers. "
                "Use this when a user only needs availability, not price data. "
                "RETRY POLICY: Call this tool ONCE. "
                "If all retailers return NOT_FOUND or in_stock is false everywhere, "
                "DO NOT retry with alternate product names — report the result directly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "description": "Product name, model number, or UPC to check availability for.",
                    },
                    "zip_code": {
                        "type": "string",
                        "description": "US ZIP code. Defaults to 10001.",
                        "default": "10001",
                    },
                },
                "required": ["product"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "product_name": {"type": "string"},
                    "amazon":       {"type": "object", "description": "Amazon availability. Contains in_stock (bool), url, confidence."},
                    "walmart":      {"type": "object", "description": "Walmart availability. Contains in_stock (bool), url, confidence."},
                    "target":       {"type": "object", "description": "Target availability. Contains in_stock (bool), url, confidence."},
                    "best_buy":     {"type": "object", "description": "Best Buy availability. Contains in_stock (bool), url, confidence."},
                    "summary":      {"type": "string"},
                },
                "required": ["product_name", "summary"],
            },
        ),
    ]


# ── Tool dispatcher ────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]):
    try:
        if name == "compare_prices":
            result = await handle_compare_prices(arguments)
            meta   = CTX_META_FULL
        elif name == "get_price_history":
            result = await handle_price_history(arguments)
            meta   = CTX_META_LIGHT
        elif name == "get_deal_score":
            result = await handle_deal_score(arguments)
            meta   = CTX_META_LIGHT
        elif name == "check_availability":
            result = await handle_availability(arguments)
            meta   = CTX_META_LIGHT
        else:
            raise ValueError(f"Unknown tool: {name}")

        result["_meta"] = meta
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, indent=2))],
            structuredContent=result,
        )

    except Exception as e:
        error_result = _error_response(name, str(e))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(error_result, indent=2))],
            structuredContent=error_result,
            isError=True,
        )


# ── Handlers ───────────────────────────────────────────────────────────────────

def _extract_size_from_query(product_query: str, explicit_size: str | None):
    """
    Defensive size extraction — handles cases where the CTX agent ignores the
    separate `size` parameter and stuffs it into the product string instead.
    e.g. "Nike Air Force 1 Low size 10.5" → product="Nike Air Force 1 Low", size="10.5"
    explicit_size always takes priority when already provided correctly.
    """
    import re as _re
    if explicit_size:
        return product_query, explicit_size
    m = _re.search(r'\b(?:size|sz)\s+([A-Za-z0-9\.]+)\b', product_query, _re.IGNORECASE)
    if m:
        size  = m.group(1)
        clean = _re.sub(r'\b(?:size|sz)\s+[A-Za-z0-9\.]+\b', '', product_query, flags=_re.IGNORECASE).strip()
        clean = _re.sub(r'\s{2,}', ' ', clean)
        return clean, size
    return product_query, None


async def handle_compare_prices(arguments: dict) -> dict:
    product_query = arguments.get("product", "").strip()
    zip_code      = arguments.get("zip_code", "10001")
    _raw_size     = arguments.get("size", "").strip() or None
    if not product_query:
        return _error_response("compare_prices", "product parameter is required")
    # Defensively extract size if agent stuffed it into the product string
    product_query, size = _extract_size_from_query(product_query, _raw_size)

    resolved       = await matcher.resolve(product_query)
    canonical_name = resolved.get("title", product_query)
    upc            = resolved.get("upc")
    brand          = resolved.get("brand")
    search_term    = resolved.get("model") or canonical_name

    amazon_raw, walmart_raw, target_raw, best_buy_raw = await asyncio.gather(
        asyncio.wait_for(fetch_amazon(search_term, upc, zip_code, size=size),   timeout=9),
        asyncio.wait_for(fetch_walmart(search_term, upc, zip_code, size=size),  timeout=7),
        asyncio.wait_for(fetch_target(search_term, upc, zip_code, size=size),   timeout=9),
        asyncio.wait_for(fetch_best_buy(search_term, upc, zip_code, size=size), timeout=9),
        return_exceptions=True,
    )

    amazon   = matcher.score_retailer_result(amazon_raw,   canonical_name, brand, requested_size=size)
    walmart  = matcher.score_retailer_result(walmart_raw,  canonical_name, brand, requested_size=size)
    target   = matcher.score_retailer_result(target_raw,   canonical_name, brand, requested_size=size)
    best_buy = matcher.score_retailer_result(best_buy_raw, canonical_name, brand, requested_size=size)

    retailer_map = {"amazon": amazon, "walmart": walmart, "target": target, "best_buy": best_buy}

    def _eff(r):
        """Return effective price (post-coupon) if available, else raw price."""
        ep = r.get("effective_price")
        p  = r.get("price")
        return ep if isinstance(ep, (int, float)) else p

    # valid_prices: all retailers with a real price (any confidence except NOT_FOUND).
    # Used for cheapest_retailer, deal scoring, spread, and verdict.
    valid_prices = {
        k: _eff(v)
        for k, v in retailer_map.items()
        if isinstance(_eff(v), (int, float)) and v.get("confidence") != "NOT_FOUND"
    }

    # cache_prices: only HIGH/MEDIUM confidence results go into price history.
    # LOW confidence results may be wrong products — don't let them set historical lows.
    cache_prices = {
        k: _eff(v)
        for k, v in retailer_map.items()
        if isinstance(_eff(v), (int, float)) and v.get("confidence") in ("HIGH", "MEDIUM")
    }

    all_found_prices = valid_prices  # alias for price_context generator

    cache.update(canonical_name, cache_prices)
    rolling_min = cache.get_rolling_min(canonical_name)
    data_points = len(cache.get_history(canonical_name))
    deal_score  = scorer.compute_deal_score(valid_prices, rolling_min, data_points)

    cheapest_retailer = cheapest_price = price_spread_pct = None
    if valid_prices:
        cheapest_retailer = min(valid_prices, key=valid_prices.get)
        cheapest_price    = valid_prices[cheapest_retailer]
        if len(valid_prices) > 1:
            max_p = max(valid_prices.values())
            min_p = min(valid_prices.values())
            if max_p > 0:
                price_spread_pct = round((max_p - min_p) / max_p * 100, 1)
    elif all_found_prices:
        # Fall back to LOW confidence so cheapest_retailer is never blank
        cheapest_retailer = min(all_found_prices, key=all_found_prices.get)
        cheapest_price    = all_found_prices[cheapest_retailer]

    verdict = scorer.generate_verdict(
        valid_prices=valid_prices or all_found_prices,
        cheapest_retailer=cheapest_retailer,
        cheapest_price=cheapest_price,
        price_spread_pct=price_spread_pct,
        deal_score=deal_score,
        rolling_min=rolling_min,
        data_points=data_points,
    )

    # Condition warning + market trend context sentence
    price_context = scorer.generate_price_context(
        valid_prices=all_found_prices,
        retailer_results=retailer_map,
        rolling_min=rolling_min,
        cheapest_price=cheapest_price,
    )

    confidences = [amazon["confidence"], walmart["confidence"], target["confidence"], best_buy["confidence"]]
    if confidences.count("HIGH") >= 2:
        overall = "HIGH"
    elif "HIGH" in confidences or confidences.count("MEDIUM") >= 2:
        overall = "MEDIUM"
    else:
        overall = "LOW"

    retailers_found = sum(1 for r in [amazon, walmart, target, best_buy] if r.get("price") is not None)

    def retailer_or_null(r):
        if r.get("confidence") == "NOT_FOUND":
            # Return a stub object instead of null so the CTX null-reflection
            # checker never fires. Always check `found` before reading price/url.
            return {
                "found":             False,
                "not_listed":        True,
                "price":             0,
                "effective_price":   0,
                "in_stock":          False,
                "url":               "",
                "title":             "",
                "confidence":        "NOT_FOUND",
                "coupon_available":  False,
                "coupon_text":       "",
                "coupon_discount":   0,
                "size_match":        False,
                "condition":         "",
            }
        result = dict(r)
        result["found"] = True
        result.setdefault("condition", "new")
        return result

    # Explain zero-result responses so CTX stops retrying the same query
    if retailers_found == 0:
        no_results_reason = (
            "no_matching_listings"
            if overall == "LOW" else
            "product_not_carried"
        )
        no_results_advice = (
            f"No listings found for '{canonical_name}' at Amazon, Walmart, Target, or Best Buy. "
            "THIS IS THE FINAL ANSWER — do not retry with different product names or variations. "
            "Present this message directly to the user: "
            f"'{canonical_name}' is not listed at major consumer retailers. "
            "It may be a business/enterprise product available through specialised channels "
            "such as the HP Business Store, Newegg, B&H Photo, or CDW."
        )
    else:
        no_results_reason = None
        no_results_advice = None

    return {
        "product_name":      canonical_name,
        "match_confidence":  overall,
        "retailers_found":   retailers_found,
        "no_results_reason": no_results_reason or "",
        "no_results_advice": no_results_advice or "",
        "search_exhausted":  retailers_found == 0,
        "amazon":            retailer_or_null(amazon),
        "walmart":           retailer_or_null(walmart),
        "target":            retailer_or_null(target),
        "best_buy":          retailer_or_null(best_buy),
        "cheapest_retailer": cheapest_retailer or "",
        "cheapest_price":    cheapest_price or 0,
        "price_spread_pct":  price_spread_pct or 0,
        "deal_score":        deal_score,
        "verdict":           verdict,
        "price_context":     price_context,
        "disclaimer": (
            "Prices queried from a fixed US location. "
            "Final prices may vary by region, membership status (Walmart+, Target Circle), "
            "and real-time availability. Verify before purchasing."
        ),
    }



async def handle_price_history(arguments: dict) -> dict:
    product = arguments.get("product", "").strip()
    if not product:
        return _error_response("get_price_history", "product parameter is required")

    history     = cache.get_history(product)
    rolling_min = cache.get_rolling_min(product)

    if not history:
        message = (
            f"No price history found for '{product}'. "
            "Run compare_prices for this product first to start building history."
        )
    else:
        message = (
            f"Found {len(history)} price observations for '{product}'. "
            f"Observed low: ${rolling_min:,.2f}."
        )

    return {
        "product":      product,
        "observed_low": rolling_min if rolling_min is not None else 0,
        "data_points":  len(history),
        "history":      history[-50:],
        "message":      message,
    }


async def handle_deal_score(arguments: dict) -> dict:
    product       = arguments.get("product", "").strip()
    current_price = arguments.get("current_price")
    if not product or current_price is None:
        return _error_response("get_deal_score", "product and current_price are required")

    rolling_min = cache.get_rolling_min(product)
    deal_score  = scorer.compute_deal_score({"provided": current_price}, rolling_min)
    verdict     = scorer.generate_verdict(
        valid_prices={"checked price": current_price},
        cheapest_retailer="checked price",
        cheapest_price=current_price,
        price_spread_pct=None,
        deal_score=deal_score,
        rolling_min=rolling_min,
    )
    return {
        "product":       product,
        "current_price": current_price,
        "deal_score":    deal_score,
        "observed_low":  rolling_min if rolling_min is not None else 0,
        "verdict":       verdict,
    }


async def handle_availability(arguments: dict) -> dict:
    product_query = arguments.get("product", "").strip()
    zip_code      = arguments.get("zip_code", "10001")
    _raw_size     = arguments.get("size", "").strip() or None
    if not product_query:
        return _error_response("check_availability", "product parameter is required")
    product_query, size = _extract_size_from_query(product_query, _raw_size)

    resolved       = await matcher.resolve(product_query)
    canonical_name = resolved.get("title", product_query)
    upc            = resolved.get("upc")
    brand          = resolved.get("brand")
    search_term    = resolved.get("model") or canonical_name

    amazon_raw, walmart_raw, target_raw, best_buy_raw = await asyncio.gather(
        asyncio.wait_for(fetch_amazon(search_term, upc, zip_code),   timeout=9),
        asyncio.wait_for(fetch_walmart(search_term, upc, zip_code),  timeout=7),
        asyncio.wait_for(fetch_target(search_term, upc, zip_code),   timeout=9),
        asyncio.wait_for(fetch_best_buy(search_term, upc, zip_code), timeout=9),
        return_exceptions=True,
    )

    amazon   = matcher.score_retailer_result(amazon_raw,   canonical_name, brand)
    walmart  = matcher.score_retailer_result(walmart_raw,  canonical_name, brand)
    target   = matcher.score_retailer_result(target_raw,   canonical_name, brand)
    best_buy = matcher.score_retailer_result(best_buy_raw, canonical_name, brand)

    def avail_label(name, r):
        if r["confidence"] == "NOT_FOUND": return f"{name}: Not found"
        s = r.get("in_stock")
        if s is True:  return f"{name}: ✅ In Stock"
        if s is False: return f"{name}: ❌ Out of Stock"
        return f"{name}: ⚠️ Unknown"

    in_stock_count = sum(
        1 for r in [amazon, walmart, target, best_buy]
        if r.get("in_stock") is True and r["confidence"] != "NOT_FOUND"
    )
    note = (
        "Available at all four retailers." if in_stock_count == 4
        else "Not confirmed in stock at any retailer." if in_stock_count == 0
        else f"In stock at {in_stock_count} of 4 retailers."
    )

    return {
        "product_name": canonical_name,
        "amazon":    {"found": amazon["confidence"] != "NOT_FOUND",    "in_stock": amazon.get("in_stock"),    "url": amazon.get("url"),    "confidence": amazon["confidence"]},
        "walmart":   {"found": walmart["confidence"] != "NOT_FOUND",   "in_stock": walmart.get("in_stock"),   "url": walmart.get("url"),   "confidence": walmart["confidence"]},
        "target":    {"found": target["confidence"] != "NOT_FOUND",    "in_stock": target.get("in_stock"),    "url": target.get("url"),    "confidence": target["confidence"]},
        "best_buy":  {"found": best_buy["confidence"] != "NOT_FOUND",  "in_stock": best_buy.get("in_stock"),  "url": best_buy.get("url"),  "confidence": best_buy["confidence"]},
        "summary": " | ".join([
            avail_label("Amazon", amazon),
            avail_label("Walmart", walmart),
            avail_label("Target", target),
            avail_label("Best Buy", best_buy),
        ]) + f" — {note}",
    }


# ── Run ────────────────────────────────────────────────────────────────────────

async def run():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="price-intelligence-feed",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(run())