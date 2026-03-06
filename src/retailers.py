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

TIMEOUT = 15  # seconds per request — well within CTX 30s limit when concurrent


# ── Amazon via SerpApi ─────────────────────────────────────────────────────────

async def fetch_amazon(search_term: str, upc: Optional[str], zip_code: str) -> dict:
    """
    Fetches Amazon product data via SerpApi.
    Returns normalised dict: { title, price, currency, in_stock, url }
    """
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not configured"}

    # Prefer ASIN lookup if we have a UPC (SerpApi can resolve UPC -> product)
    query = upc if upc else search_term

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # First: search for the product
            search_resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":   "amazon",
                    "k":        query,
                    "amazon_domain": "amazon.com",
                    "api_key":  SERPAPI_KEY,
                },
            )
            search_data = search_resp.json()

            # Extract first organic result
            results = search_data.get("organic_results", [])
            if not results:
                return {"error": "No Amazon results found"}

            top = results[0]
            asin = top.get("asin")

            # Second: get full product details for accurate price + stock
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

            # Fallback to search result data
            return _parse_amazon_search_result(top)

    except httpx.TimeoutException:
        return {"error": "Amazon request timed out"}
    except Exception as e:
        return {"error": f"Amazon fetch error: {str(e)}"}


def _parse_amazon_product(product: dict, asin: str) -> dict:
    # buying_options can be a list of dicts OR a list of strings depending on
    # SerpApi response format — guard against both
    buying_options = product.get("buying_options", [])
    first_option   = buying_options[0] if buying_options else {}
    pricing        = first_option if isinstance(first_option, dict) else {}

    # Try multiple price fields in order of reliability
    price_str = (
        pricing.get("price")
        or (product.get("price", {}).get("value") if isinstance(product.get("price"), dict) else product.get("price"))
        or (product.get("typical_price_range", [None])[0] if product.get("typical_price_range") else None)
    )

    availability = product.get("availability", "")
    in_stock = isinstance(availability, str) and availability.lower() not in (
        "currently unavailable", "out of stock", "unavailable"
    )

    return {
        "title":    product.get("title", ""),
        "price":    _parse_price(str(price_str)) if price_str else None,
        "currency": "USD",
        "in_stock": in_stock,
        "url":      f"https://www.amazon.com/dp/{asin}",
    }


def _parse_amazon_search_result(result: dict) -> dict:
    return {
        "title":    result.get("title", ""),
        "price":    _parse_price(str(result.get("price", {}).get("value", ""))),
        "currency": "USD",
        "in_stock": True,  # assume in stock if showing in search
        "url":      result.get("link", ""),
    }


# ── Walmart via ScraperAPI ─────────────────────────────────────────────────────

async def fetch_walmart(search_term: str, upc: Optional[str], zip_code: str) -> dict:
    """
    Fetches Walmart product data via ScraperAPI (renders JS, bypasses bot detection).
    """
    if not SCRAPERAPI_KEY:
        return {"error": "SCRAPERAPI_KEY not configured"}

    query = upc if upc else search_term
    walmart_search_url = f"https://www.walmart.com/search?q={query.replace(' ', '+')}"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # ScraperAPI structured data endpoint for Walmart
            resp = await client.get(
                "https://api.scraperapi.com/structured/walmart/search",
                params={
                    "api_key":    SCRAPERAPI_KEY,
                    "query":      query,
                    "country_code": "us",
                },
            )
            data = resp.json()

            organic = data.get("organic_results", [])
            if not organic:
                return {"error": "No Walmart results found"}

            top = organic[0]
            return {
                "title":    top.get("name", ""),
                "price":    _parse_price(str(top.get("price", ""))),
                "currency": "USD",
                "in_stock": top.get("available_for_delivery", True),
                "url":      f"https://www.walmart.com{top['url']}" if top.get("url") else walmart_search_url,
            }

    except httpx.TimeoutException:
        return {"error": "Walmart request timed out"}
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
                    "api_key":   REDCIRCLE_KEY,
                    "search_term": query,
                    "type":      "search",
                    "retailer":  "target",
                    "zip_code":  zip_code,
                },
            )
            data = resp.json()

            results = data.get("search_results", [])
            if not results:
                return {"error": "No Target results found"}

            top = results[0].get("product", {})
            offer = results[0].get("offers", {}).get("primary", {})

            return {
                "title":    top.get("title", ""),
                "price":    _parse_price(str(offer.get("price", ""))),
                "currency": "USD",
                "in_stock": offer.get("availability", "").lower() not in ("out of stock", "unavailable"),
                "url":      top.get("link", ""),
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