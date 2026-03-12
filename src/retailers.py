"""
Retailer data fetchers
- Amazon   via SerpApi  — organic-only results, coupon extraction, sponsored filtering
- Walmart  via ScraperAPI — model-number query optimisation, coupon extraction
- Target   via RedCircle API
- Best Buy via SerpApi  — same key as Amazon, electronics specialist

All functions are async and run concurrently via asyncio.gather().
All accept an optional `size` param which is appended to the search query
and used by the matcher to validate variant correctness.
"""

import os
import re
import httpx
from typing import Optional

SERPAPI_KEY    = os.getenv("SERPAPI_KEY", "")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "")
REDCIRCLE_KEY  = os.getenv("REDCIRCLE_KEY", "")

TIMEOUT         = 15   # seconds — well within CTX 30s limit when concurrent
WALMART_TIMEOUT = 6    # hard cap: return NOT_FOUND rather than blow the CTX budget


# ── Shared helpers ─────────────────────────────────────────────────────────────

REFURBISHED_KEYWORDS = [
    "renewed", "refurbished", "used", "open box", "open-box",
    "pre-owned", "preowned", "certified refurbished", "seller refurbished",
]

def _detect_condition(title: str) -> str:
    title_lower = title.lower()
    for kw in REFURBISHED_KEYWORDS:
        if kw in title_lower:
            return "renewed" if "renew" in kw else "used"
    return "new"

def _parse_price(price_str: str) -> Optional[float]:
    """Extract a float from '$279.99', '279.99', '$279', etc."""
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(price_str))
    try:
        return round(float(cleaned), 2) if cleaned else None
    except ValueError:
        return None

def _build_query(search_term: str, size: Optional[str]) -> str:
    """Append size to search query when provided, e.g. 'Air Jordan 1 Low size 11'."""
    if size:
        return f"{search_term} size {size}"
    return search_term

def _extract_coupon_amazon(product: dict) -> tuple[bool, str, Optional[float]]:
    """
    Extract coupon data from SerpApi Amazon product result.
    Returns (coupon_available, coupon_text, coupon_discount_amount).
    SerpApi exposes coupons under product_results.coupon or buying_options[].coupon.
    """
    # Direct coupon field on product
    coupon = product.get("coupon", {})
    if isinstance(coupon, dict) and coupon:
        text   = coupon.get("text", "") or coupon.get("badge_text", "")
        amount = _parse_price(str(coupon.get("discount_amount", "") or coupon.get("amount", "")))
        if text or amount:
            return True, text or f"${amount} off", amount

    # Coupon on buying options
    buying_options = product.get("buying_options", [])
    for opt in (buying_options if isinstance(buying_options, list) else []):
        if isinstance(opt, dict):
            opt_coupon = opt.get("coupon", {})
            if isinstance(opt_coupon, dict) and opt_coupon:
                text   = opt_coupon.get("text", "") or opt_coupon.get("badge_text", "")
                amount = _parse_price(str(opt_coupon.get("discount_amount", "") or opt_coupon.get("amount", "")))
                if text or amount:
                    return True, text or f"${amount} off", amount

    return False, "", None


def _extract_coupon_walmart(item: dict) -> tuple[bool, str, Optional[float]]:
    """
    Extract coupon/savings from ScraperAPI Walmart result.
    Walmart exposes savings_amount, rollback_price, or coupon fields.
    """
    savings = item.get("savings_amount") or item.get("savings") or item.get("price_drop")
    if savings:
        amount = _parse_price(str(savings))
        if amount and amount > 0:
            return True, f"${amount:.2f} savings", amount

    rollback = item.get("rollback_price") or item.get("was_price")
    regular  = item.get("price") or item.get("sale_price")
    if rollback and regular:
        rb = _parse_price(str(rollback))
        rg = _parse_price(str(regular))
        if rb and rg and rb > rg:
            diff = round(rb - rg, 2)
            return True, f"Was ${rb:.2f}, now ${rg:.2f} (${diff:.2f} off)", diff

    return False, "", None


# ── Amazon via SerpApi ─────────────────────────────────────────────────────────

async def fetch_amazon(
    search_term: str,
    upc: Optional[str],
    zip_code: str,
    size: Optional[str] = None,
) -> dict:
    """
    Fetches Amazon product data via SerpApi.
    - Uses organic_results only (sponsored results filtered out)
    - Extracts coupon/discount data
    - Appends size to query when provided
    Returns: { title, price, currency, in_stock, condition, coupon_available,
               coupon_text, coupon_discount, effective_price, url }
    """
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not configured"}

    base_query = upc if upc else search_term
    query      = _build_query(base_query, size)

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

            # ── Feature 5: Sponsored filtering ────────────────────────────
            # SerpApi separates organic_results from ads_results / sponsored_results.
            # Always use organic_results. Skip any result with is_sponsored=True.
            organic = [
                r for r in search_data.get("organic_results", [])
                if not r.get("is_sponsored", False)
            ]
            if not organic:
                return {"error": "No organic Amazon results found"}

            top  = organic[0]
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

    title                             = product.get("title", "")
    price                             = _parse_price(str(price_str)) if price_str else None
    coupon_available, coupon_text, coupon_discount = _extract_coupon_amazon(product)
    effective_price = (
        round(price - coupon_discount, 2)
        if price and coupon_discount and coupon_discount > 0
        else price
    )

    return {
        "title":            title,
        "price":            price,
        "currency":         "USD",
        "in_stock":         in_stock,
        "condition":        _detect_condition(title),
        "coupon_available": coupon_available,
        "coupon_text":      coupon_text,
        "coupon_discount":  coupon_discount,
        "effective_price":  effective_price,
        "url":              f"https://www.amazon.com/dp/{asin}",
    }


def _parse_amazon_search_result(result: dict) -> dict:
    title = result.get("title", "")
    price = _parse_price(str(result.get("price", {}).get("value", "")))
    return {
        "title":            title,
        "price":            price,
        "currency":         "USD",
        "in_stock":         True,
        "condition":        _detect_condition(title),
        "coupon_available": False,
        "coupon_text":      "",
        "coupon_discount":  None,
        "effective_price":  price,
        "url":              result.get("link", ""),
    }


# ── Best Buy via SerpApi ───────────────────────────────────────────────────────

async def fetch_best_buy(
    search_term: str,
    upc: Optional[str],
    zip_code: str,
    size: Optional[str] = None,
) -> dict:
    """
    Fetches Best Buy product data via SerpApi best_buy engine.
    Uses the same SERPAPI_KEY as Amazon — no extra API key needed.
    Best Buy is the leading retailer for electronics deals and often
    undercuts Amazon on headphones, laptops, TVs, and gaming.
    Returns: { title, price, currency, in_stock, condition, coupon_available,
               coupon_text, coupon_discount, effective_price, url }
    """
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not configured"}

    base_query = upc if upc else search_term
    query      = _build_query(base_query, size)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":  "best_buy",
                    "q":       query,
                    "api_key": SERPAPI_KEY,
                },
            )
            data = resp.json()

            # SerpApi Best Buy returns results under 'organic_results'
            results = [
                r for r in data.get("organic_results", [])
                if not r.get("is_sponsored", False)
            ]
            if not results:
                return {"error": "No Best Buy results found"}

            top   = results[0]
            title = top.get("title", "")
            price = _parse_price(str(
                top.get("price") or
                top.get("sale_price") or
                top.get("regular_price") or ""
            ))

            # Best Buy sale badge — treat as coupon equivalent
            sale_price   = _parse_price(str(top.get("sale_price", "") or ""))
            regular_price = _parse_price(str(top.get("regular_price", "") or ""))
            coupon_available = False
            coupon_text      = ""
            coupon_discount  = None

            if sale_price and regular_price and regular_price > sale_price:
                diff             = round(regular_price - sale_price, 2)
                coupon_available = True
                coupon_text      = f"On sale: was ${regular_price:.2f}, now ${sale_price:.2f} (${diff:.2f} off)"
                coupon_discount  = diff
                price            = sale_price  # use the sale price

            effective_price = (
                round(price - coupon_discount, 2)
                if price and coupon_discount
                else price
            )

            in_stock = top.get("in_stock", True)
            if isinstance(in_stock, str):
                in_stock = in_stock.lower() not in ("false", "out of stock", "unavailable")

            url = top.get("link") or top.get("url") or ""

            return {
                "title":            title,
                "price":            price,
                "currency":         "USD",
                "in_stock":         bool(in_stock),
                "condition":        _detect_condition(title),
                "coupon_available": coupon_available,
                "coupon_text":      coupon_text,
                "coupon_discount":  coupon_discount,
                "effective_price":  effective_price,
                "url":              url,
            }

    except httpx.TimeoutException:
        return {"error": "Best Buy request timed out"}
    except Exception as e:
        return {"error": f"Best Buy fetch error: {str(e)}"}


# ── Walmart via ScraperAPI ─────────────────────────────────────────────────────

async def fetch_walmart(
    search_term: str,
    upc: Optional[str],
    zip_code: str,
    size: Optional[str] = None,
) -> dict:
    """
    Fetches Walmart product data via ScraperAPI structured endpoint.
    Hard timeout of 6s — returns error rather than blowing the CTX budget.
    Extracts coupon/savings data when available.
    """
    if not SCRAPERAPI_KEY:
        return {"error": "SCRAPERAPI_KEY not configured"}

    raw_query   = upc if upc else search_term
    model_match = re.search(r"[A-Z]{1,5}[-_]?[0-9]{2,6}[A-Z0-9-]*", search_term, re.IGNORECASE)
    base_query  = model_match.group(0) if model_match and not upc else raw_query
    query       = _build_query(base_query, size)
    walmart_search_url = f"https://www.walmart.com/search?q={query.replace(' ', '+')}"

    try:
        async with httpx.AsyncClient(timeout=WALMART_TIMEOUT) as client:
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
                return {"error": f"No Walmart results found (keys: {list(data.keys()) if isinstance(data, dict) else []})"}

            top       = organic[0]
            title     = top.get("name") or top.get("title") or ""
            raw_price = top.get("price") or top.get("sale_price") or ""
            price     = _parse_price(str(raw_price))
            in_stock  = (
                top.get("available_for_delivery")
                or top.get("in_stock")
                or top.get("availabilityStatus", "").lower() == "in_stock"
                or True
            )
            url_path = top.get("url") or top.get("product_url") or ""

            coupon_available, coupon_text, coupon_discount = _extract_coupon_walmart(top)
            effective_price = (
                round(price - coupon_discount, 2)
                if price and coupon_discount and coupon_discount > 0
                else price
            )

            return {
                "title":            title,
                "price":            price,
                "currency":         "USD",
                "in_stock":         bool(in_stock),
                "condition":        _detect_condition(title),
                "coupon_available": coupon_available,
                "coupon_text":      coupon_text,
                "coupon_discount":  coupon_discount,
                "effective_price":  effective_price,
                "url":              f"https://www.walmart.com{url_path}" if url_path.startswith("/") else (url_path or walmart_search_url),
            }

    except httpx.TimeoutException:
        return {"error": f"Walmart timed out after {WALMART_TIMEOUT}s"}
    except Exception as e:
        return {"error": f"Walmart fetch error: {str(e)}"}


# ── Target via RedCircle API ───────────────────────────────────────────────────

async def fetch_target(
    search_term: str,
    upc: Optional[str],
    zip_code: str,
    size: Optional[str] = None,
) -> dict:
    """
    Fetches Target product data via RedCircle API.
    """
    if not REDCIRCLE_KEY:
        return {"error": "REDCIRCLE_KEY not configured"}

    base_query = upc if upc else search_term
    query      = _build_query(base_query, size)

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
            data    = resp.json()
            results = data.get("search_results", [])
            if not results:
                return {"error": "No Target results found"}

            top   = results[0].get("product", {})
            offer = results[0].get("offers", {}).get("primary", {})
            title = top.get("title", "")
            price = _parse_price(str(offer.get("price", "")))

            # Target promotions
            promo            = offer.get("promotion", {}) or {}
            coupon_available = bool(promo)
            coupon_text      = promo.get("description", "") or promo.get("label", "")
            coupon_discount  = _parse_price(str(promo.get("savings_amount", "") or ""))
            effective_price  = (
                round(price - coupon_discount, 2)
                if price and coupon_discount and coupon_discount > 0
                else price
            )

            return {
                "title":            title,
                "price":            price,
                "currency":         "USD",
                "in_stock":         offer.get("availability", "").lower() not in ("out of stock", "unavailable"),
                "condition":        _detect_condition(title),
                "coupon_available": coupon_available,
                "coupon_text":      coupon_text,
                "coupon_discount":  coupon_discount,
                "effective_price":  effective_price,
                "url":              top.get("link", ""),
            }

    except httpx.TimeoutException:
        return {"error": "Target request timed out"}
    except Exception as e:
        return {"error": f"Target fetch error: {str(e)}"}