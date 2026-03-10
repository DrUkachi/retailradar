"""
Retailer data fetchers
- Amazon  via SerpApi (handles anti-bot, structured JSON)
- Walmart via ScraperAPI (public page scraping)
- Target  via RedCircle API (structured product data)

All functions are async and designed to run concurrently.
API keys are loaded from environment variables.
"""

import os
import re
import httpx
from typing import Optional

SERPAPI_KEY    = os.getenv("SERPAPI_KEY", "")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "")
REDCIRCLE_KEY  = os.getenv("REDCIRCLE_KEY", "")

TIMEOUT = 15  # seconds per request

REFURBISHED_KEYWORDS = [
    "renewed", "refurbished", "used", "open box", "open-box",
    "pre-owned", "preowned", "certified refurbished", "seller refurbished",
]

def _detect_condition(title: str) -> str:
    """Returns 'new', 'renewed', or 'used' based on title keywords."""
    title_lower = title.lower()
    for kw in REFURBISHED_KEYWORDS:
        if kw in title_lower:
            return "renewed" if "renew" in kw else "used"
    return "new"


# ── Amazon via SerpApi ─────────────────────────────────────────────────────────

async def fetch_amazon(search_term: str, upc: Optional[str], zip_code: str) -> dict:
    """
    Fetches Amazon product data via SerpApi.
    Returns normalised dict: { title, price, currency, in_stock, condition, url }
    """
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not configured"}

    query = upc if upc else search_term

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            search_resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":        "amazon",
                    "k":             query,
                    "amazon_domain": "amazon.com",
                    "api_key":       SERPAPI_KEY,
                },
            )
            search_data = search_resp.json()

            results = search_data.get("organic_results", [])
            if not results:
                return {"error": "No Amazon results found"}

            top  = results[0]
            asin = top.get("asin")

            if asin:
                detail_resp = await client.get(
                    "https://serpapi.com/search.json",
                    params={
                        "engine":        "amazon_product",
                        "asin":          asin,
                        "amazon_domain": "amazon.com",
                        "api_key":       SERPAPI_KEY,
                    },
                )
                detail_data = detail_resp.json()
                product = detail_data.get("product_results", {})
                if product:
                    return _parse_amazon_product(product, asin)

            return _parse_amazon_search_result(top)

    except httpx.TimeoutException:
        return {"error": "Amazon request timed out"}
    except Exception as e:
        return {"error": f"Amazon fetch error: {str(e)}"}


def _parse_amazon_product(product: dict, asin: str) -> dict:
    buying_options = product.get("buying_options", [])
    first_option   = buying_options[0] if buying_options else {}
    pricing        = first_option if isinstance(first_option, dict) else {}

    price_str = (
        pricing.get("price")
        or (product.get("price", {}).get("value") if isinstance(product.get("price"), dict) else product.get("price"))
        or (product.get("typical_price_range", [None])[0] if product.get("typical_price_range") else None)
    )

    availability = product.get("availability", "")
    in_stock = isinstance(availability, str) and availability.lower() not in (
        "currently unavailable", "out of stock", "unavailable"
    )

    title = product.get("title", "")
    return {
        "title":     title,
        "price":     _parse_price(str(price_str)) if price_str else None,
        "currency":  "USD",
        "in_stock":  in_stock,
        "condition": _detect_condition(title),
        "url":       f"https://www.amazon.com/dp/{asin}",
    }


def _parse_amazon_search_result(result: dict) -> dict:
    title = result.get("title", "")
    return {
        "title":     title,
        "price":     _parse_price(str(result.get("price", {}).get("value", ""))),
        "currency":  "USD",
        "in_stock":  True,
        "condition": _detect_condition(title),
        "url":       result.get("link", ""),
    }


# ── Walmart via ScraperAPI ─────────────────────────────────────────────────────

async def fetch_walmart(search_term: str, upc: Optional[str], zip_code: str) -> dict:
    """
    Fetches Walmart product data via ScraperAPI structured endpoint.
    Uses model number extraction for cleaner search results.
    """
    if not SCRAPERAPI_KEY:
        return {"error": "SCRAPERAPI_KEY not configured"}

    # Extract model number (e.g. WH-1000XM5) for cleaner Walmart search results.
    # "WH-1000XM5" returns the right product; "Sony WH-1000XM5 Wireless Noise..." does not.
    raw_query   = upc if upc else search_term
    model_match = re.search(r"[A-Z]{1,5}[-_]?[0-9]{2,6}[A-Z0-9-]*", search_term, re.IGNORECASE)
    query       = model_match.group(0) if model_match and not upc else raw_query
    walmart_search_url = f"https://www.walmart.com/search?q={query.replace(' ', '+')}"

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.get(
                "https://api.scraperapi.com/structured/walmart/search",
                params={
                    "api_key":      SCRAPERAPI_KEY,
                    "query":        query,
                    "country_code": "us",
                },
            )

            if resp.status_code != 200:
                return {"error": f"Walmart API error: HTTP {resp.status_code}"}

            data    = resp.json()
            organic = data.get("organic_results") or data.get("items") or []
            if not organic:
                keys = list(data.keys()) if isinstance(data, dict) else []
                return {"error": f"No Walmart results found (response keys: {keys})"}

            top       = organic[0]
            title     = top.get("name") or top.get("title") or ""
            raw_price = top.get("price") or top.get("sale_price") or ""
            in_stock  = (
                top.get("available_for_delivery")
                or top.get("in_stock")
                or top.get("availabilityStatus", "").lower() == "in_stock"
                or True
            )
            url_path = top.get("url") or top.get("product_url") or ""
            return {
                "title":     title,
                "price":     _parse_price(str(raw_price)),
                "currency":  "USD",
                "in_stock":  bool(in_stock),
                "condition": _detect_condition(title),
                "url":       f"https://www.walmart.com{url_path}" if url_path.startswith("/") else (url_path or walmart_search_url),
            }

    except httpx.TimeoutException:
        return {"error": "Walmart request timed out after 25s"}
    except Exception as e:
        return {"error": f"Walmart fetch error: {str(e)}"}


# ── Target via RedCircle API ───────────────────────────────────────────────────

async def fetch_target(search_term: str, upc: Optional[str], zip_code: str) -> dict:
    """
    Fetches Target product data via RedCircle API.
    """
    if not REDCIRCLE_KEY:
        return {"error": "REDCIRCLE_KEY not configured"}

    query = upc if upc else search_term

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                "https://api.redcircleapi.com/request",
                params={
                    "api_key":     REDCIRCLE_KEY,
                    "search_term": query,
                    "type":        "search",
                    "retailer":    "target",
                    "zip_code":    zip_code,
                },
            )
            data = resp.json()

            results = data.get("search_results", [])
            if not results:
                return {"error": "No Target results found"}

            top   = results[0].get("product", {})
            offer = results[0].get("offers", {}).get("primary", {})
            title = top.get("title", "")
            return {
                "title":     title,
                "price":     _parse_price(str(offer.get("price", ""))),
                "currency":  "USD",
                "in_stock":  offer.get("availability", "").lower() not in ("out of stock", "unavailable"),
                "condition": _detect_condition(title),
                "url":       top.get("link", ""),
            }

    except httpx.TimeoutException:
        return {"error": "Target request timed out"}
    except Exception as e:
        return {"error": f"Target fetch error: {str(e)}"}


# ── Shared utility ─────────────────────────────────────────────────────────────

def _parse_price(price_str: str) -> Optional[float]:
    """Extract a float price from strings like '$279.99', '279.99', '$279'."""
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.]", "", price_str)
    try:
        return round(float(cleaned), 2) if cleaned else None
    except ValueError:
        return None
