"""
Product Matching Engine
Five-tier waterfall:
  1. UPC/barcode lookup via UPCitemdb
  2. Model number extraction
  3. Semantic similarity via fastembed (BAAI/bge-small-en-v1.5)
     — catches category mismatches that fuzzy scoring misses
     — e.g. "Air Jordan 1 Low" vs "Apple AirPods 4" are semantically distant
  4. RapidFuzz fuzzy title matching (catches typos, word order)
  5. Graceful partial result

The semantic model is loaded ONCE at module import time so Railway's first
cold-start takes ~3s longer but subsequent requests add only ~5ms each.
If the model fails to load (e.g. missing package), the system falls back
gracefully to fuzzy-only matching.
"""

import re
import os
import logging
import httpx
from typing import Any
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

UPC_API_BASE = "https://api.upcitemdb.com/prod/trial/lookup"

FUZZY_HIGH_THRESHOLD   = 80
FUZZY_MEDIUM_THRESHOLD = 50
MINIMUM_FUZZY_SCORE    = 25

# Cosine similarity threshold for semantic match.
# Empirical values using all-MiniLM-L6-v2:
#   "Air Jordan 1 Low" vs "Nike Men's Air Jordan 1 Low Sneaker"  → ~0.82  ✅ pass
#   "Air Jordan 1 Low" vs "Apple AirPods 4 Wireless Earbuds"    → ~0.18  ❌ reject
#   "Sony WH-1000XM5"  vs "Sony WH-1000XM4 Headphones"          → ~0.79  ✅ pass
#   "Sony WH-1000XM5"  vs "Sony WH-1000XM5 Carrying Case"       → ~0.41  ✅ pass (accessory filter handles this)
#   "Jordan 1 Mid"     vs "Apple AirPods 4 Wireless Earbuds"    → ~0.15  ❌ reject
SEMANTIC_REJECT_THRESHOLD = 0.35

# ── Load semantic model once at startup ───────────────────────────────────────

_semantic_model = None
_semantic_available = False

def _load_semantic_model():
    """
    Load fastembed model at startup. fastembed is PyTorch-free (~50MB),
    installs fast on Railway, and produces high-quality embeddings via ONNX.
    Model: BAAI/bge-small-en-v1.5 — 384 dimensions, same as MiniLM.
    Falls back silently to fuzzy-only if not installed.
    """
    global _semantic_model, _semantic_available
    try:
        from fastembed import TextEmbedding
        _semantic_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        # Warm up the model with a dummy encode so first real request is fast
        list(_semantic_model.embed(["warmup"]))
        _semantic_available = True
        logger.info("Semantic matcher loaded: BAAI/bge-small-en-v1.5 (fastembed)")
    except Exception as e:
        logger.warning(f"Semantic matcher unavailable (falling back to fuzzy-only): {e}")
        _semantic_available = False

# Load on import — happens once when Railway boots the server
_load_semantic_model()


def _cosine_similarity(vec_a, vec_b) -> float:
    """Compute cosine similarity between two numpy vectors."""
    import numpy as np
    dot   = np.dot(vec_a, vec_b)
    norm  = np.linalg.norm(vec_a) * np.linalg.norm(vec_b)
    return float(dot / norm) if norm > 0 else 0.0


def semantic_similarity(query: str, result_title: str) -> float | None:
    """
    Returns cosine similarity [0.0–1.0] between query and result title.
    Returns None if semantic model is not available (caller should skip check).
    fastembed.embed() returns a generator — convert to list to materialise.
    """
    if not _semantic_available or _semantic_model is None:
        return None
    try:
        import numpy as np
        embeddings = list(_semantic_model.embed([query, result_title]))
        vec_a = np.array(embeddings[0])
        vec_b = np.array(embeddings[1])
        return _cosine_similarity(vec_a, vec_b)
    except Exception as e:
        logger.warning(f"Semantic similarity failed: {e}")
        return None


# ── Existing matching infrastructure ──────────────────────────────────────────

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
        """
        query = query.strip()

        if re.fullmatch(r"\d{8}|\d{12}|\d{13}", query):
            result = await self._upc_lookup(query)
            if result:
                return result

        model = self._extract_model(query)
        if model:
            return {
                "title": query,
                "upc":   None,
                "brand": self._extract_brand(query),
                "model": model,
            }

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
        words = text.split()
        for w in words:
            if w and w[0].isupper() and len(w) > 1:
                return w
        return ""

    def _extract_model(self, text: str) -> str:
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
        Takes a raw retailer fetch result and returns a normalised dict
        with confidence score attached.

        Scoring pipeline:
          1. Error / empty check
          2. Accessory mismatch filter (keyword-based, fast)
          3. Semantic similarity check (NLP — rejects category mismatches)
          4. Fuzzy score (catches typos / word order)
          5. Variant mismatch penalty
          6. Confidence assignment
        """
        if isinstance(raw, Exception) or raw is None:
            return self._not_found(note=str(raw) if isinstance(raw, Exception) else "No result")

        if not isinstance(raw, dict):
            return self._not_found(note="Unexpected response format")

        if raw.get("error"):
            return self._not_found(note=raw["error"])

        retailer_title = raw.get("title", "")
        if not retailer_title:
            return self._not_found(note="No title in response")

        # ── Step 1: Accessory mismatch (fast keyword check) ───────────────
        accessory_flag = self._is_accessory_mismatch(canonical_title, retailer_title)
        if accessory_flag:
            return self._not_found(
                note=(
                    f"Result appears to be an accessory, not the product itself: "
                    f"'{retailer_title[:80]}'"
                )
            )

        # ── Step 2: Semantic similarity (NLP category mismatch check) ─────
        # This is the main fix for "AirPods returned for Jordan shoe query".
        # The semantic model understands that shoes and earbuds are different
        # categories even when they share words like "Air".
        sem_score = semantic_similarity(canonical_title, retailer_title)
        if sem_score is not None and sem_score < SEMANTIC_REJECT_THRESHOLD:
            return self._not_found(
                note=(
                    f"Result rejected: semantic similarity {sem_score:.2f} below threshold "
                    f"{SEMANTIC_REJECT_THRESHOLD} (category mismatch). "
                    f"Query: '{canonical_title[:50]}' — Got: '{retailer_title[:60]}'"
                )
            )

        # ── Step 3: Fuzzy score (typos, word order, partial matches) ──────
        fuzzy_score = self._fuzzy_score(canonical_title, retailer_title)

        if fuzzy_score < MINIMUM_FUZZY_SCORE:
            return self._not_found(
                note=f"Result rejected: fuzzy score {fuzzy_score:.0f} below minimum threshold. Got: '{retailer_title[:60]}'"
            )

        # ── Step 4: Variant mismatch penalty ──────────────────────────────
        user_variants     = self._extract_variants(canonical_title)
        retailer_variants = self._extract_variants(retailer_title)
        variant_warning   = self._variant_mismatch(user_variants, retailer_variants)

        # ── Step 5: Confidence assignment ─────────────────────────────────
        if fuzzy_score >= FUZZY_HIGH_THRESHOLD and not variant_warning:
            confidence = "HIGH"
        elif fuzzy_score >= FUZZY_MEDIUM_THRESHOLD:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        note = variant_warning or ""

        return {
            "price":          raw.get("price"),
            "currency":       raw.get("currency", "USD"),
            "in_stock":       raw.get("in_stock"),
            "url":            raw.get("url"),
            "title":          retailer_title,
            "confidence":     confidence,
            "fuzzy_score":    fuzzy_score,
            "semantic_score": round(sem_score, 3) if sem_score is not None else None,
            "note":           note,
        }

    def _fuzzy_score(self, a: str, b: str) -> float:
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
        Returns True ONLY if the result is clearly an accessory FOR the queried
        product — shares query words but adds an accessory keyword.
        Does NOT flag completely unrelated products (that's the semantic layer's job).
        """
        query_lower = query.lower()
        title_lower = retailer_title.lower()

        stopwords   = {"the", "for", "and", "with", "gen", "new"}
        query_words = [w for w in re.findall(r"[a-z0-9]+", query_lower)
                       if len(w) >= 3 and w not in stopwords]

        title_contains_query_words = any(w in title_lower for w in query_words)
        if not title_contains_query_words:
            return False

        for keyword in ACCESSORY_KEYWORDS:
            if keyword in title_lower and keyword not in query_lower:
                return True
        return False

    def _not_found(self, note: str = "") -> dict:
        return {
            "price":          None,
            "currency":       "USD",
            "in_stock":       False,
            "url":            "",
            "title":          "",
            "confidence":     "NOT_FOUND",
            "fuzzy_score":    0,
            "semantic_score": None,
            "note":           note or "Product not found at this retailer",
        }
