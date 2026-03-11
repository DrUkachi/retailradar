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
                "Surfaces active coupons and sale prices so the effective_price after discounts is always shown. "
                "Use this when a user wants to know where to buy a product for the best price today, "
                "or whether the current price is a good deal. "
                "Accepts a product name like 'Sony WH-1000XM5', a model number, a UPC barcode, "
                "or a product with size like 'Air Jordan 1 Low size 11'. "
                "Pass the size parameter separately when the user specifies a size or fit."
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
                    "zip_code": {
                        "type": "string",
                        "description": "US ZIP code for localised pricing. Defaults to 10001 (New York).",
                        "default": "10001",
                    },
                    "size": {
                        "type": "string",
                        "description": "Optional size for shoes or clothing, e.g. '11', '10.5', 'XL'. When provided, appended to search query and used to validate retailer results contain the right size.",
                    },
                },
                "required": ["product"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "product_name":      {"type": "string"},
                    "match_confidence":  {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "retailers_found":   {"type": "integer", "description": "Number of retailers (0-4) that returned a valid price. Use this to know if data is complete."},
                    "amazon":            {"type": ["object", "null"], "description": "Amazon price result. Contains price, in_stock, url, title, confidence, condition ('new'|'renewed'|'used'). Null if no matching product found."},
                    "walmart":           {"type": ["object", "null"], "description": "Walmart price result. Contains price, in_stock, url, title, confidence, condition ('new'|'renewed'|'used'). Null if no matching product found."},
                    "target":            {"type": ["object", "null"], "description": "Target price result. Contains price, in_stock, url, title, confidence, condition ('new'|'renewed'|'used'). Null if no matching product found."},
                    "cheapest_retailer": {"type": "string", "description": "Name of cheapest retailer. Empty string if no prices found."},
                    "cheapest_price":    {"type": "number", "description": "Cheapest price found. 0 if no prices found."},
                    "price_spread_pct":  {"type": "number", "description": "Percentage difference between highest and lowest price. 0 when fewer than 2 retailers returned prices."},
                    "deal_score":        {"type": "integer", "minimum": 0, "maximum": 10},
                    "verdict":           {"type": "string"},
                    "price_context":          {"type": "string", "description": "Condition warning and market trend note. Empty string if nothing notable."},
                    "size_requested":          {"type": ["string", "null"], "description": "The size passed by the caller, if any."},
                    "best_deal_with_coupon":   {"type": ["object", "null"], "description": "If any retailer has an active coupon, this shows the retailer with the lowest effective_price after discount. Null if no coupons active."},
                    "disclaimer":             {"type": "string"},
                },
                "required": ["product_name", "match_confidence", "deal_score", "verdict", "price_context", "disclaimer", "retailers_found"],
            },
        ),

        # Tool 2: Price history
        types.Tool(
            name="get_price_history",
            description=(
                "Retrieve the observed price history for a product from the local cache. "
                "Returns all previously recorded prices across Amazon, Walmart, and Target, "
                "including the all-time observed low price and how many data points have been collected. "
                "Use this when a user wants to understand how a product's price has changed over time, "
                "or to verify whether a current price is genuinely a good deal historically. "
                "Note: history only exists for products previously queried via compare_prices."
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
                    "observed_low": {"type": ["number", "null"]},
                    "data_points":  {"type": "integer"},
                    "history":      {"type": "array"},
                    "message":      {"type": "string"},
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
                    "observed_low":  {"type": ["number", "null"]},
                    "verdict":       {"type": "string"},
                },
                "required": ["product", "current_price", "deal_score", "verdict"],
            },
        ),

        # Tool 4: Availability check
        types.Tool(
            name="check_availability",
            description=(
                "Check whether a product is currently in stock at Amazon, Walmart, and Target. "
                "Use this when a user wants to know if a product is available to buy right now "
                "without needing full price comparison data. "
                "Returns in-stock status and a direct product URL for each retailer. "
                "Faster than compare_prices when the user only cares about stock status, "
                "for example: 'Is the PS5 in stock anywhere right now?'"
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
                    "amazon":       {"type": "object"},
                    "walmart":      {"type": "object"},
                    "target":       {"type": "object"},
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

async def handle_compare_prices(arguments: dict) -> dict:
    product_query  = arguments.get("product", "").strip()
    zip_code       = arguments.get("zip_code", "10001")
    size           = arguments.get("size", "").strip() or None
    if not product_query:
        return _error_response("compare_prices", "product parameter is required")

    resolved       = await matcher.resolve(product_query)
    canonical_name = resolved.get("title", product_query)
    upc            = resolved.get("upc")
    brand          = resolved.get("brand")
    search_term    = resolved.get("model") or canonical_name

    # ── Fetch all 4 retailers concurrently ─────────────────────────────────
    amazon_raw, walmart_raw, target_raw, best_buy_raw = await asyncio.gather(
        fetch_amazon(search_term, upc, zip_code, size=size),
        fetch_walmart(search_term, upc, zip_code, size=size),
        fetch_target(search_term, upc, zip_code, size=size),
        fetch_best_buy(search_term, upc, zip_code, size=size),
        return_exceptions=True,
    )

    amazon   = matcher.score_retailer_result(amazon_raw,    canonical_name, brand, requested_size=size)
    walmart  = matcher.score_retailer_result(walmart_raw,   canonical_name, brand, requested_size=size)
    target   = matcher.score_retailer_result(target_raw,    canonical_name, brand, requested_size=size)
    best_buy = matcher.score_retailer_result(best_buy_raw,  canonical_name, brand, requested_size=size)

    retailer_map = {"amazon": amazon, "walmart": walmart, "target": target, "best_buy": best_buy}

    # ── Use effective_price (after coupons) for deal scoring ───────────────
    def _eff(r):
        """Return effective price (post-coupon) if available, else raw price."""
        ep = r.get("effective_price")
        p  = r.get("price")
        return ep if isinstance(ep, (int, float)) else p

    valid_prices = {
        k: _eff(v)
        for k, v in retailer_map.items()
        if isinstance(_eff(v), (int, float)) and v.get("confidence") in ("HIGH", "MEDIUM")
    }

    all_found_prices = {
        k: _eff(v)
        for k, v in retailer_map.items()
        if isinstance(_eff(v), (int, float)) and v.get("confidence") != "NOT_FOUND"
    }

    cache.update(canonical_name, valid_prices)
    rolling_min = cache.get_rolling_min(canonical_name)
    deal_score  = scorer.compute_deal_score(valid_prices, rolling_min)

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
        cheapest_retailer = min(all_found_prices, key=all_found_prices.get)
        cheapest_price    = all_found_prices[cheapest_retailer]

    verdict = scorer.generate_verdict(
        valid_prices=valid_prices or all_found_prices,
        cheapest_retailer=cheapest_retailer,
        cheapest_price=cheapest_price,
        price_spread_pct=price_spread_pct,
        deal_score=deal_score,
        rolling_min=rolling_min,
    )

    price_context = scorer.generate_price_context(
        valid_prices=all_found_prices,
        retailer_results=retailer_map,
        rolling_min=rolling_min,
        cheapest_price=cheapest_price,
    )

    confidences = [r["confidence"] for r in retailer_map.values()]
    if confidences.count("HIGH") >= 2:
        overall = "HIGH"
    elif "HIGH" in confidences or confidences.count("MEDIUM") >= 2:
        overall = "MEDIUM"
    else:
        overall = "LOW"

    retailers_found = sum(1 for r in retailer_map.values() if r.get("price") is not None)

    def retailer_or_null(r):
        if r.get("confidence") == "NOT_FOUND":
            return None
        result = dict(r)
        result.setdefault("condition", "new")
        result.setdefault("coupon_available", False)
        result.setdefault("coupon_text", "")
        result.setdefault("coupon_discount", None)
        result.setdefault("effective_price", result.get("price"))
        result.setdefault("size_match", None)
        return result

    # ── best_deal_with_coupon: lowest effective price where coupon is active ──
    coupon_options = {
        k: _eff(v)
        for k, v in retailer_map.items()
        if v.get("coupon_available") and isinstance(_eff(v), (int, float))
    }
    best_coupon = None
    if coupon_options:
        best_coupon_retailer = min(coupon_options, key=coupon_options.get)
        best_coupon_data     = retailer_map[best_coupon_retailer]
        best_coupon = {
            "retailer":        best_coupon_retailer,
            "effective_price": coupon_options[best_coupon_retailer],
            "coupon_text":     best_coupon_data.get("coupon_text", ""),
            "url":             best_coupon_data.get("url", ""),
        }

    return {
        "product_name":           canonical_name,
        "match_confidence":       overall,
        "retailers_found":        retailers_found,
        "amazon":                 retailer_or_null(amazon),
        "walmart":                retailer_or_null(walmart),
        "target":                 retailer_or_null(target),
        "best_buy":               retailer_or_null(best_buy),
        "cheapest_retailer":      cheapest_retailer or "",
        "cheapest_price":         cheapest_price or 0,
        "price_spread_pct":       price_spread_pct or 0,
        "deal_score":             deal_score,
        "verdict":                verdict,
        "price_context":          price_context,
        "size_requested":         size,
        "best_deal_with_coupon":  best_coupon,
        "disclaimer": (
            "Prices queried from a fixed US location. "
            "Effective prices reflect active coupons/sales at time of query. "
            "Final prices may vary by region, membership status (Walmart+, Target Circle, My Best Buy), "
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
        "observed_low": rolling_min,
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
        "observed_low":  rolling_min,
        "verdict":       verdict,
    }


async def handle_availability(arguments: dict) -> dict:
    product_query = arguments.get("product", "").strip()
    zip_code      = arguments.get("zip_code", "10001")
    if not product_query:
        return _error_response("compare_prices", "product parameter is required")

    resolved       = await matcher.resolve(product_query)
    canonical_name = resolved.get("title", product_query)
    upc            = resolved.get("upc")
    brand          = resolved.get("brand")
    search_term    = resolved.get("model") or canonical_name

    amazon_raw, walmart_raw, target_raw = await asyncio.gather(
        fetch_amazon(search_term, upc, zip_code),
        fetch_walmart(search_term, upc, zip_code),
        fetch_target(search_term, upc, zip_code),
        return_exceptions=True,
    )

    amazon  = matcher.score_retailer_result(amazon_raw,  canonical_name, brand)
    walmart = matcher.score_retailer_result(walmart_raw, canonical_name, brand)
    target  = matcher.score_retailer_result(target_raw,  canonical_name, brand)

    def avail_label(name, r):
        if r["confidence"] == "NOT_FOUND": return f"{name}: Not found"
        s = r.get("in_stock")
        if s is True:  return f"{name}: ✅ In Stock"
        if s is False: return f"{name}: ❌ Out of Stock"
        return f"{name}: ⚠️ Unknown"

    in_stock_count = sum(
        1 for r in [amazon, walmart, target]
        if r.get("in_stock") is True and r["confidence"] != "NOT_FOUND"
    )
    note = (
        "Available at all three retailers." if in_stock_count == 3
        else "Not confirmed in stock at any retailer." if in_stock_count == 0
        else f"In stock at {in_stock_count} of 3 retailers."
    )

    return {
        "product_name": canonical_name,
        "amazon":  {"in_stock": amazon.get("in_stock"),  "url": amazon.get("url"),  "confidence": amazon["confidence"]},
        "walmart": {"in_stock": walmart.get("in_stock"), "url": walmart.get("url"), "confidence": walmart["confidence"]},
        "target":  {"in_stock": target.get("in_stock"),  "url": target.get("url"),  "confidence": target["confidence"]},
        "summary": " | ".join([
            avail_label("Amazon", amazon),
            avail_label("Walmart", walmart),
            avail_label("Target", target),
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
