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
import logging
import re
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

SERPAPI_KEY    = os.getenv("SERPAPI_KEY", "")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "")
REDCIRCLE_KEY  = os.getenv("REDCIRCLE_KEY", "")

# Per-retailer timeouts — tight enough to fail fast, not so tight we miss slow APIs
# asyncio.wait_for wraps each coroutine individually so one slow retailer
# doesn't block the others (unlike a shared timeout on asyncio.gather).
AMAZON_TIMEOUT   = 13  # SerpApi free tier can be slow — give headroom under wait_for(14)
WALMART_TIMEOUT  = 9   # ScraperAPI — under wait_for(10)
TARGET_TIMEOUT   = 13
BESTBUY_TIMEOUT  = 13

def _make_client(timeout: float) -> httpx.AsyncClient:
    """Create an httpx client with connection pooling and keep-alive enabled."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=4.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        http2=False,  # avoid h2 negotiation overhead on SerpApi
    )


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

def _parse_price(price_str: str, max_usd: float = 50_000.0) -> Optional[float]:
    """Extract a float from '$279.99', '279.99', '$279', etc.
    Rejects values above max_usd to filter out foreign-currency amounts
    (e.g. NGN 1,041,782 parsed as 1041782.22 would be rejected).
    """
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(price_str))
    try:
        value = round(float(cleaned), 2) if cleaned else None
        if value and value > max_usd:
            return None  # Likely a foreign currency amount, not USD
        return value
    except ValueError:
        return None

def _extract_serpapi_price(result: dict, max_usd: float = 50_000.0) -> Optional[float]:
    """
    Robustly extract price from a SerpApi organic result dict.
    SerpApi returns prices in multiple formats depending on the engine and product:
      - {"price": {"value": "279.99"}}   — nested dict with value key
      - {"price": {"raw": "$279.99"}}    — nested dict with raw key
      - {"price": "$279.99"}             — string with currency symbol
      - {"price": 279.99}                — bare float
      - {"extracted_price": 279.99}      — top-level extracted_price field
      - {"price_string": "$279.99"}      — price_string field
    """
    # Try top-level extracted_price first (most reliable)
    extracted = result.get("extracted_price")
    if isinstance(extracted, (int, float)) and 0 < extracted < max_usd:
        return round(float(extracted), 2)

    price_raw = result.get("price")

    # Nested dict
    if isinstance(price_raw, dict):
        for key in ("value", "raw", "extracted", "amount"):
            val = price_raw.get(key)
            if val is not None:
                cleaned = re.sub(r"[^\d.]", "", str(val))
                try:
                    v = round(float(cleaned), 2) if cleaned else None
                    if v and 0 < v < max_usd:
                        return v
                except ValueError:
                    pass

    # String or numeric
    if price_raw is not None and not isinstance(price_raw, dict):
        cleaned = re.sub(r"[^\d.]", "", str(price_raw))
        try:
            v = round(float(cleaned), 2) if cleaned else None
            if v and 0 < v < max_usd:
                return v
        except ValueError:
            pass

    # Try price_string field
    price_string = result.get("price_string") or result.get("price_str") or ""
    if price_string:
        cleaned = re.sub(r"[^\d.]", "", str(price_string))
        try:
            v = round(float(cleaned), 2) if cleaned else None
            if v and 0 < v < max_usd:
                return v
        except ValueError:
            pass

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
        async with _make_client(AMAZON_TIMEOUT) as client:
            search_resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":        "amazon",
                    "k":             query,
                    "amazon_domain": "amazon.com",
                    "gl":            "us",
                    "hl":            "en",
                    "location":      "United States",
                    "api_key":       SERPAPI_KEY,
                },
            )
            search_data = search_resp.json()

            # Use organic results only — skip sponsored
            organic = [
                r for r in search_data.get("organic_results", [])
                if not r.get("is_sponsored", False)
            ]
            if not organic:
                return {"error": "No organic Amazon results found"}

            # Use search result directly — skip the second detail API call.
            # The detail call added coupon data but doubled latency (2 serial
            # SerpApi calls = 16s+ before gather could resolve Amazon).
            # Search results contain title, price, ASIN and URL — sufficient.
            top = organic[0]
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
        "coupon_discount":  coupon_discount or 0,
        "effective_price":  effective_price,
        "url":              f"https://www.amazon.com/dp/{asin}",
    }


def _parse_amazon_search_result(result: dict) -> dict:
    title = result.get("title", "")
    price = _extract_serpapi_price(result)
    return {
        "title":            title,
        "price":            price,
        "currency":         "USD",
        "in_stock":         True,
        "condition":        _detect_condition(title),
        "coupon_available": False,
        "coupon_text":      "",
        "coupon_discount":  0,
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
        async with _make_client(BESTBUY_TIMEOUT) as client:
            # Use Google Shopping with site:bestbuy.com in query
            # More reliable than engine=best_buy (unofficial/inconsistent)
            # or merchant ID filter (returns empty for many products)
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":  "google_shopping",
                    "q":       f"{query} site:bestbuy.com",
                    "gl":      "us",
                    "hl":      "en",
                    "api_key": SERPAPI_KEY,
                },
            )
            data = resp.json()

            # Filter to Best Buy sourced results only
            results = [
                r for r in data.get("shopping_results", [])
                if not r.get("is_sponsored", False)
                and "best buy" in (r.get("source", "") or "").lower()
            ]
            # Fallback: any non-sponsored shopping result
            if not results:
                results = [
                    r for r in data.get("shopping_results", [])
                    if not r.get("is_sponsored", False)
                ]
            if not results:
                return {"error": "No Best Buy results found"}

            top   = results[0]
            title = top.get("title", "")
            price = _extract_serpapi_price(top) or _parse_price(str(
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

            # Prefer actual Best Buy product URL over Google Shopping redirect
            raw_url = top.get("product_link") or top.get("url") or top.get("link") or ""
            if raw_url and "bestbuy.com" in raw_url:
                url = raw_url
            else:
                url = f"https://www.bestbuy.com/site/searchpage.jsp?st={query.replace(' ', '+')}"

            return {
                "title":            title,
                "price":            price,
                "currency":         "USD",
                "in_stock":         bool(in_stock),
                "condition":        _detect_condition(title),
                "coupon_available": coupon_available,
                "coupon_text":      coupon_text,
                "coupon_discount":  coupon_discount or 0,
                "effective_price":  effective_price,
                "url":              url,
            }

    except httpx.TimeoutException:
        return {"error": "Best Buy request timed out"}
    except Exception as e:
        return {"error": f"Best Buy fetch error: {str(e)}"}


async def _fetch_best_buy_fallback(
    search_term: str,
    size: Optional[str],
) -> dict:
    """
    Fallback: fetch Best Buy pricing via Google Shopping with Best Buy merchant filter.
    Used when engine=best_buy returns no results (inconsistent SerpApi engine).
    Best Buy merchant ID on Google Shopping: m100000125
    """
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not configured"}

    query = _build_query(search_term, size)

    try:
        async with _make_client(BESTBUY_TIMEOUT) as client:
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":  "google_shopping",
                    "q":       query + " Best Buy",  # site-hint in query
                    "gl":      "us",
                    "hl":      "en",
                    "api_key": SERPAPI_KEY,
                },
            )
            data = resp.json()

            results = [
                r for r in data.get("shopping_results", [])
                if not r.get("is_sponsored", False)
                and "best buy" in (r.get("source", "") or "").lower()
            ]
            if not results:
                results = [
                    r for r in data.get("shopping_results", [])
                    if not r.get("is_sponsored", False)
                ]
            if not results:
                return {"error": "No Best Buy results via Google Shopping fallback"}

            top   = results[0]
            title = top.get("title", "")
            price = _extract_serpapi_price(top)
            url   = (top.get("product_link") or top.get("link") or
                     f"https://www.bestbuy.com/site/searchpage.jsp?st={query.replace(' ', '+')}")

            return {
                "title":            title,
                "price":            price,
                "currency":         "USD",
                "in_stock":         True,
                "condition":        _detect_condition(title),
                "coupon_available": False,
                "coupon_text":      "",
                "coupon_discount":  0,
                "effective_price":  price,
                "url":              url,
            }

    except Exception as e:
        return {"error": f"Best Buy fallback error: {str(e)}"}


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

    # Use the full search_term (canonical product name) — do NOT strip to model number only.
    # Bare model numbers like "WH-1000XM5" without "Sony" cause Walmart to return
    # wrong products (accessories, bundles, successor models).
    raw_query = upc if upc else search_term
    query     = _build_query(raw_query, size)
    walmart_search_url = f"https://www.walmart.com/search?q={query.replace(' ', '+')}"

    try:
        async with _make_client(WALMART_TIMEOUT) as client:
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

            # Pick best result from top 5 using fuzzy title matching.
            # This is dynamic — no hardcoded bundle keywords needed.
            # A standalone product ("Sony WH-1000XM5 Headphones") scores higher
            # than a bundle ("Sony WH-1000XM5 + Protection Plan + Power Bank")
            # because bundle titles add noise tokens that don't match the query.
            from rapidfuzz import fuzz as _fuzz

            def _quick_score(query_str: str, title: str) -> float:
                """Fast token_set_ratio — handles word order and extra tokens."""
                q = query_str.lower()
                t = title.lower()
                return _fuzz.token_set_ratio(q, t)

            candidates = organic[:5]
            scored = [
                (candidate, _quick_score(search_term, candidate.get("name") or candidate.get("title") or ""))
                for candidate in candidates
            ]
            # Sort by score descending — best match first
            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[0][0]

            title     = top.get("name") or top.get("title") or ""
            raw_price = top.get("price") or top.get("sale_price") or ""
            price     = _parse_price(str(raw_price)) or _extract_serpapi_price(top)
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
                "coupon_discount":  coupon_discount or 0,
                "effective_price":  effective_price,
                "url":              f"https://www.walmart.com{url_path}" if url_path.startswith("/") else (url_path or walmart_search_url),
            }

    except httpx.TimeoutException:
        return {"error": f"Walmart timed out after {WALMART_TIMEOUT}s"}
    except Exception as e:
        return {"error": f"Walmart fetch error: {str(e)}"}


async def _fetch_walmart_fallback(
    search_term: str,
    size: Optional[str],
) -> dict:
    """
    Fallback: fetch Walmart pricing via Google Shopping with Walmart merchant filter.
    Used when ScraperAPI returns no usable result (e.g. only bundles or wrong model).
    Walmart merchant ID on Google Shopping: m107903633
    Runs WITHIN the Walmart coroutine slot — does not extend total gather() time.
    """
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not configured"}

    query = _build_query(search_term, size)

    try:
        async with _make_client(WALMART_TIMEOUT) as client:
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine":  "google_shopping",
                    "q":       query + " Walmart",  # site-hint in query
                    "gl":      "us",
                    "hl":      "en",
                    "api_key": SERPAPI_KEY,
                },
            )
            data = resp.json()

            results = [
                r for r in data.get("shopping_results", [])
                if not r.get("is_sponsored", False)
                and "walmart" in (r.get("source", "") or "").lower()
            ]
            if not results:
                results = [
                    r for r in data.get("shopping_results", [])
                    if not r.get("is_sponsored", False)
                ]
            if not results:
                return {"error": "No Walmart results via Google Shopping fallback"}

            # Pick best match by fuzzy score — avoids bundles dynamically
            from rapidfuzz import fuzz as _fuzz
            results_scored = sorted(
                results,
                key=lambda r: _fuzz.token_set_ratio(search_term.lower(), (r.get("title") or "").lower()),
                reverse=True
            )
            top   = results_scored[0]
            title = top.get("title", "")
            price = _extract_serpapi_price(top)
            # Prefer direct walmart.com product URL; never use Google redirect as the URL
            raw_url = top.get("product_link") or top.get("link") or ""
            if raw_url and "walmart.com" in raw_url:
                url = raw_url
            else:
                url = f"https://www.walmart.com/search?q={query.replace(' ', '+')}"

            return {
                "title":            title,
                "price":            price,
                "currency":         "USD",
                "in_stock":         True,
                "condition":        _detect_condition(title),
                "coupon_available": False,
                "coupon_text":      "",
                "coupon_discount":  0,
                "effective_price":  price,
                "url":              url,
            }

    except Exception as e:
        return {"error": f"Walmart fallback error: {str(e)}"}


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
        async with _make_client(TARGET_TIMEOUT) as client:
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
            if resp.status_code == 401:
                return {"error": "Target API: REDCIRCLE_KEY is invalid or expired. Renew at redcircleapi.com"}
            if resp.status_code == 403:
                return {"error": "Target API: REDCIRCLE_KEY access denied — check plan limits at redcircleapi.com"}
            if resp.status_code != 200:
                return {"error": f"Target API error: HTTP {resp.status_code}"}

            data    = resp.json()

            # Handle RedCircle auth failure response
            request_info = data.get("request_info", {})
            if not request_info.get("success", True):
                return {"error": f"Target API failed: {request_info.get('message', 'unknown error')}"}

            results = data.get("search_results", [])
            logger.info(f"RedCircle Target: HTTP {resp.status_code}, keys={list(data.keys())}, results={len(results)}")
            if not results:
                return {"error": f"No Target results found. Response keys: {list(data.keys())}"}

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
                "coupon_discount":  coupon_discount or 0,
                "effective_price":  effective_price,
                "url":              top.get("link", ""),
            }

    except httpx.TimeoutException:
        return {"error": "Target request timed out"}
    except Exception as e:
        return {"error": f"Target fetch error: {str(e)}"}