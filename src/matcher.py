"""
Product Matching Engine
Four-tier waterfall:
  1. UPC/barcode lookup via UPCitemdb
  2. Model number extraction
  3. RapidFuzz fuzzy title matching
  4. Graceful partial result
"""

import re
import os
import httpx
from typing import Any
from rapidfuzz import fuzz

UPC_API_BASE = "https://api.upcitemdb.com/prod/trial/lookup"
FUZZY_HIGH_THRESHOLD   = 80
FUZZY_MEDIUM_THRESHOLD = 50

VARIANT_PATTERNS = {
    "storage": r"(\d+\s*(?:gb|tb))",
    "color":   r"\b(black|white|silver|gold|blue|red|pink|graphite|midnight|starlight|purple|green|yellow|coral)\b",
    "size":    r"(\d+\.?\d*\s*(?:inch|inches|\"|ft|oz|fl oz|lb|liter|ml|mm|cm))",
    "year":    r"\b(20\d{2})\b",
    "gen":     r"\b(\d+(?:st|nd|rd|th)\s*gen(?:eration)?)\b",
}

STOPWORDS = {
    "wireless", "headphones", "earbuds", "earphones", "speaker", "speakers",
    "black", "white", "silver", "gold", "bundle", "new", "sealed", "refurbished",
    "renewed", "the", "with", "for", "and", "in", "a", "an", "of",
    "latest", "model", "version", "edition", "pack", "set",
}

# Keywords that indicate a result is an accessory, not the main product.
# If the user query does not mention any of these words but the result title
# does, the result is flagged as an accessory mismatch and confidence drops to LOW.
ACCESSORY_KEYWORDS = [
    "case", "cover", "screen protector", "tempered glass", "charger",
    "cable", "stand", "skin", "sleeve", "pouch", "holder", "mount",
    "adapter", "dock", "stylus", "protector", "bumper", "shell",
    "wallet", "folio", "kickstand", "magsafe", "band", "strap",
    "film", "wrap", "decal", "sticker", "clip", "hook", "grip",
]


class ProductMatcher:
    """Resolves a user query into a canonical product identity and scores retailer results."""

    async def resolve(self, query: str) -> dict:
        """
        Returns: { title, upc, brand, model }

        Strategy:
        - If the query looks like a barcode (8/12/13 digits), do a UPC lookup
        - If the query looks like a model number (letters+digits), skip UPC search
          and go straight to retailer search — UPCitemdb free tier returns wrong
          products for popular electronics like iPhones, AirPods, etc.
        - Otherwise extract brand/model from the raw query string
        """
        query = query.strip()

        # Is the query itself a UPC barcode? (8, 12, or 13 digits only)
        if re.fullmatch(r"\d{8}|\d{12}|\d{13}", query):
            result = await self._upc_lookup(query)
            if result:
                return result

        # Does the query look like a model number? (e.g. WH-1000XM5, RTX4090)
        # If so, skip UPC search entirely — use the query directly as the search term
        model = self._extract_model(query)
        if model:
            return {
                "title": query,   # use the full original query as canonical title
                "upc":   None,
                "brand": self._extract_brand(query),
                "model": model,
            }

        # For plain text queries (e.g. "Sony headphones", "iPhone 15 Pro Max"),
        # skip UPCitemdb entirely — its free tier consistently returns wrong products
        # for popular electronics. Use the query directly as the search term.
        return {
            "title": query,
            "upc":   None,
            "brand": self._extract_brand(query),
            "model": self._extract_model(query),
        }

    async def _upc_lookup(self, upc: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(UPC_API_BASE, params={"upc": upc})
                data = r.json()
                items = data.get("items", [])
                if items:
                    item = items[0]
                    return {
                        "title": item.get("title", ""),
                        "upc":   upc,
                        "brand": item.get("brand", ""),
                        "model": item.get("model", ""),
                    }
        except Exception:
            pass
        return None

    async def _upc_search(self, query: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    "https://api.upcitemdb.com/prod/trial/search",
                    params={"s": query, "type": "product"},
                )
                data = r.json()
                items = data.get("items", [])
                if items:
                    item = items[0]
                    upcs = item.get("upc", [])
                    return {
                        "title": item.get("title", query),
                        "upc":   upcs[0] if upcs else None,
                        "brand": item.get("brand", ""),
                        "model": item.get("model", ""),
                    }
        except Exception:
            pass
        return None

    def _extract_brand(self, text: str) -> str:
        """Heuristic: first capitalised word is often the brand."""
        words = text.split()
        for w in words:
            if w and w[0].isupper() and len(w) > 1:
                return w
        return ""

    def _extract_model(self, text: str) -> str:
        """Extract model-number-like substrings (letters + digits combos)."""
        # e.g. WH-1000XM5, RTX4090, MBP-14-M3
        pattern = r"\b[A-Z]{1,5}[-_]?\d{2,6}[A-Z0-9\-]*\b"
        matches = re.findall(pattern, text, re.IGNORECASE)
        return matches[0] if matches else ""

    # ── Retailer result scoring ──────────────────────────────────────────────

    def score_retailer_result(
        self,
        raw: Any,
        canonical_title: str,
        brand: str | None,
    ) -> dict:
        """
        Takes a raw retailer fetch result (dict or Exception) and returns
        a normalised dict with confidence score attached.
        """
        # Handle fetch errors or exceptions gracefully
        if isinstance(raw, Exception) or raw is None:
            return self._not_found(note=str(raw) if isinstance(raw, Exception) else "No result")

        if not isinstance(raw, dict):
            return self._not_found(note="Unexpected response format")

        if raw.get("error"):
            return self._not_found(note=raw["error"])

        retailer_title = raw.get("title", "")
        if not retailer_title:
            return self._not_found(note="No title in response")

        # Fuzzy match
        score = self._fuzzy_score(canonical_title, retailer_title)

        # Variant mismatch penalty
        user_variants     = self._extract_variants(canonical_title)
        retailer_variants = self._extract_variants(retailer_title)
        variant_warning   = self._variant_mismatch(user_variants, retailer_variants)

        # Accessory mismatch check
        # Runs BEFORE confidence assignment — an accessory result is always LOW
        # confidence even if the fuzzy score is high, because the product title
        # technically contains all the query words (e.g. "iPhone 15 Pro Max Case"
        # scores 100 against "iPhone 15 Pro Max" but is clearly the wrong product)
        accessory_flag = self._is_accessory_mismatch(canonical_title, retailer_title)
        if accessory_flag:
            return self._not_found(
                note=(
                    f"⚠️ Result appears to be an accessory, not the product itself: "
                    f"'{retailer_title[:80]}'. Try a more specific query."
                )
            )

        # Assign confidence
        if score >= FUZZY_HIGH_THRESHOLD and not variant_warning:
            confidence = "HIGH"
        elif score >= FUZZY_MEDIUM_THRESHOLD:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        note = variant_warning or None

        return {
            "price":      raw.get("price"),
            "currency":   raw.get("currency", "USD"),
            "in_stock":   raw.get("in_stock"),
            "url":        raw.get("url"),
            "title":      retailer_title,
            "confidence": confidence,
            "fuzzy_score": score,
            "note":       note,
        }

    def _fuzzy_score(self, a: str, b: str) -> int:
        a_norm = self._normalise(a)
        b_norm = self._normalise(b)
        return fuzz.token_sort_ratio(a_norm, b_norm)

    def _normalise(self, text: str) -> str:
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        tokens = [t for t in text.split() if t not in STOPWORDS and len(t) > 1]
        return " ".join(sorted(tokens))

    def _extract_variants(self, text: str) -> dict:
        variants = {}
        for key, pattern in VARIANT_PATTERNS.items():
            m = re.search(pattern, text.lower())
            if m:
                variants[key] = m.group(1).strip()
        return variants

    def _variant_mismatch(self, user: dict, retailer: dict) -> str | None:
        mismatches = []
        for key in ("storage", "size", "year", "gen"):
            u = user.get(key)
            r = retailer.get(key)
            if u and r and u.lower() != r.lower():
                mismatches.append(f"{key}: query={u}, result={r}")
        if mismatches:
            return "⚠️ Possible variant mismatch — " + "; ".join(mismatches)
        return None


    def _is_accessory_mismatch(self, query: str, retailer_title: str) -> bool:
        """
        Returns True if the retailer result appears to be an accessory
        but the user query is not asking for one.

        How it works:
          - Loops through every word in ACCESSORY_KEYWORDS
          - If the retailer title contains that word AND the user query does not
            contain that word, the result is an accessory mismatch
          - Example: query="iphone 15 pro max", title="iPhone 15 Pro Max Case"
            -> "case" is in title but not in query -> mismatch -> return True
          - Example: query="iphone 15 pro max case", title="iPhone 15 Pro Max Case"
            -> "case" is in both -> user wants a case -> return False
        """
        query_lower = query.lower()
        title_lower = retailer_title.lower()
        for keyword in ACCESSORY_KEYWORDS:
            if keyword in title_lower and keyword not in query_lower:
                return True
        return False

    def _not_found(self, note: str = "") -> dict:
        return {
            "price":       None,
            "currency":    "USD",
            "in_stock":    None,
            "url":         None,
            "title":       None,
            "confidence":  "NOT_FOUND",
            "fuzzy_score": 0,
            "note":        note or "Product not found on this retailer",
        }