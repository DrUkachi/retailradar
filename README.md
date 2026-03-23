# RetailRadar — Multi-Retailer Price Intelligence MCP

A Python MCP (Model Context Protocol) server that compares real-time product prices across Amazon, Walmart, Target, and Best Buy — returning the cheapest retailer, a deal score (0–10), historical price context, and a plain-English buy/wait recommendation.

**Replaces:** Keepa Pro (€19–€459/month) · Jungle Scout ($49–$129/month)

---

## Table of Contents

1. [Overview](#overview)
2. [Features](#features)
3. [Architecture](#architecture)
4. [Product Matching Pipeline](#product-matching-pipeline)
5. [Prerequisites](#prerequisites)
6. [Installation](#installation)
7. [Configuration](#configuration)
8. [Running the Server](#running-the-server)
9. [MCP Tools Reference](#mcp-tools-reference)
10. [HTTP Endpoints](#http-endpoints)
11. [Integration with LLM Clients](#integration-with-llm-clients)
12. [CTX Protocol Authentication](#ctx-protocol-authentication)
13. [Testing](#testing)
14. [Deployment](#deployment)
15. [CTX Protocol Registration](#ctx-protocol-registration)
16. [Project Structure](#project-structure)
17. [Contributing](#contributing)
18. [License](#license)

---

## Overview

RetailRadar is an MCP server built with Python that enables large language models and AI assistants to perform real-time product price comparisons across the four largest US consumer retailers. It normalises product names, resolves identifiers, tracks price history, and produces a structured response with actionable recommendations — all within a single tool call.

**Live deployment:** Actively serving queries on the [CTX Protocol marketplace](https://ctxprotocol.com).  
**Response time:** 12–20 seconds for a full four-retailer comparison on a cold query (within the CTX Protocol 60-second platform limit). Repeat queries within a 10-minute window are served instantly from the response cache.  
**Tool ID:** `4bf2fc7a-0863-43f9-95f2-6f3b437ab6aa`

---

## Features

- **Real-time cross-retailer price comparison** across Amazon, Walmart, Target, and Best Buy
- **5-tier product matching waterfall:** UPC/barcode → model number extraction → semantic embeddings → smart fuzzy scoring → graceful partial result
- **Smart fuzzy scoring** — normalises model number hyphens (`WH-1000XM5` and `WH1000XM5` score identically), uses `max(token_sort, partial, token_set)` ratios to handle short queries against long retailer titles, and applies a **definitive model-number conflict rejection** — wrong variants (XM5 vs XM4, G10 vs G9) receive score 0 and are rejected as `NOT_FOUND` rather than passed through at LOW confidence
- **Dynamic bundle detection** — all candidate results are scored against the query with `token_set_ratio`; bundles and add-ons ("+ Protection Plan", "+ Power Bank") score lower than clean standalone listings and are skipped automatically, with **no hardcoded bundle keywords**
- **Size-aware matching** for footwear and clothing — validates size from retailer titles and defensively extracts size when passed inline in the product string
- **Deal scoring (0–10)** based on live prices vs. historical rolling minimums, with honest thin-history handling (requires ≥3 HIGH/MEDIUM confidence observations before drawing conclusions)
- **Calibrated historical verdicts** — LOW confidence matches are shown to users but excluded from price history so wrong-product prices never corrupt the observed historical low
- **Plain-English verdicts** — buy-now-or-wait recommendations with supporting price context
- **Coupon detection** — extracts and computes effective prices after coupons and sale badges
- **Availability checking** across all four retailers per ZIP code
- **10-minute response cache** — repeat queries served instantly without hitting downstream APIs
- **Rolling price history cache** — deal score accuracy improves over time with zero external dependencies
- **Per-retailer confidence levels** (`HIGH` / `MEDIUM` / `LOW` / `NOT_FOUND`) — every data point is auditable
- **Fully async architecture** — all four retailer fetches run concurrently via `asyncio.gather()` with individual `asyncio.wait_for()` timeouts so a slow retailer never blocks the others
- **CTX Protocol SDK authentication** — `verify_context_request` on `tools/call`; discovery methods remain open
- **Typed response schema** — all fields always present with typed defaults; `size_match` is always a string (`"N/A"`, `"MATCH"`, or `"MISMATCH"`), never null
- **Dual transport support** — HTTP/SSE (web deployments and CTX Protocol) and stdio (local LLM clients)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         LLM / AI Client                             │
│           (Claude Desktop, Continue, CTX Protocol, etc.)            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  MCP Protocol (HTTP/SSE or stdio)
┌───────────────────────────────▼─────────────────────────────────────┐
│                        app.py  (ASGI)                               │
│   MCPRouter → /mcp  ──► CTX Auth ──► StreamableHTTPSessionManager  │
│             → /health, /debug ──► Starlette routes                  │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────────┐
│                       server.py  (MCP Tools)                        │
│  ┌──────────────┐  ┌─────────────────┐  ┌──────────┐  ┌─────────┐  │
│  │compare_prices│  │get_price_history │  │get_deal  │  │check_   │  │
│  │              │  │                 │  │_score    │  │availabi │  │
│  └──────┬───────┘  └────────┬────────┘  └────┬─────┘  │lity     │  │
│         │                   │                │         └────┬────┘  │
└─────────┼───────────────────┼────────────────┼──────────────┼───────┘
          │  asyncio.gather() + asyncio.wait_for() per retailer
┌─────────▼───────────────────▼────────────────▼──────────────▼───────┐
│                          src/  (Core Modules)                        │
│                                                                      │
│  matcher.py      retailers.py    scorer.py        cache.py           │
│  (5-tier         (Amazon /       (0–10 deal       (rolling price     │
│  product         Walmart /       score + plain-   minimum + 10-min  │
│  matching)       Target /        English verdict) response cache)   │
│                  Best Buy)                                           │
└──────────────────────────────────────────────────────────────────────┘
```

**Data providers:**

| Retailer | Provider | Notes |
|---|---|---|
| Amazon | SerpApi (`engine=amazon`) | Organic results only — sponsored listings filtered |
| Best Buy | SerpApi (`engine=google_shopping`, `site:bestbuy.com`) | More reliable than the unofficial `engine=best_buy` |
| Walmart | ScraperAPI structured endpoint + Google Shopping fallback | Dynamic fuzzy scoring picks best result from top 5 candidates |
| Target | RedCircle API | ZIP-code localised pricing; paid plan required |

---

## Product Matching Pipeline

| Tier | Method | Detail |
|---|---|---|
| 1 | UPC/barcode lookup | Via UPCItemdb API — exact product identity |
| 2 | Model number extraction | Regex extraction of alphanumeric model codes |
| 3 | Semantic similarity | `fastembed` BAAI/bge-small-en-v1.5 — rejects category mismatches (shoes vs earbuds) |
| 4 | Smart fuzzy scoring | RapidFuzz with model-number normalisation and conflict detection |
| 5 | Graceful partial result | Returns best available match with `LOW` confidence |

**Model conflict rejection:**  
When the query and a retailer result share a model number root but differ in suffix (e.g. `wh1000xm5` vs `wh1000xm6`), the fuzzy score is forced to 0 and the result is immediately rejected as `NOT_FOUND`. This prevents a Sony WH-1000XM6 being returned for a WH-1000XM5 query, or an HP ProBook G9 for a G10 query.

**Dynamic bundle detection:**  
All top-5 candidates from a retailer are scored with `token_set_ratio` against the search query. A bundle listing such as "Sony WH-1000XM5 + 2-Year Amber Protection Plan" scores ~42 against `"Sony WH-1000XM5"` while a clean listing scores 100. The highest-scoring candidate is always selected, making bundle rejection robust to any add-on pattern without code changes.

---

## Prerequisites

- Python 3.10+
- API keys for the four data providers (see [Configuration](#configuration))

---

## Installation

```bash
git clone https://github.com/DrUkachi/context-mcp.git
cd context-mcp
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt
```

---

## Configuration

### API Keys

```bash
cp .env.example .env
```

| Variable | Provider | Notes |
|---|---|---|
| `SERPAPI_KEY` | serpapi.com | Amazon & Best Buy. 100 searches/month free; pay-as-you-go available |
| `SCRAPERAPI_KEY` | scraperapi.com | Walmart primary. 5,000 credits/month free |
| `REDCIRCLE_KEY` | redcircleapi.com | Target. **Paid plan required** — free trial has limited product coverage |
| `CTX_AUDIENCE` | — | Your deployment URL + `/mcp`. Scopes CTX JWT verification to your instance |
| `PORT` | — | HTTP server port (default: `8000`) |

> **Important:** If `REDCIRCLE_KEY` is missing or invalid, Target queries silently return `NOT_FOUND` on every request — the fetcher returns immediately without calling the RedCircle API. Verify with `/debug`.

### Verify Configuration

```bash
curl http://localhost:8000/debug
```

All three providers should show `true`:

```json
{
  "serpapi_loaded": true,
  "scraperapi_loaded": true,
  "redcircle_loaded": true
}
```

---

## Running the Server

### HTTP Mode (recommended)

```bash
python app.py
```

### stdio Mode (for Claude Desktop and local LLM clients)

```bash
python -m server
```

---

## MCP Tools Reference

All response fields are always present — no null values. `size_match` is always a string.

---

### 1. `compare_prices`

Full cross-retailer price comparison with deal scoring. Queries all four retailers concurrently in a single call.

> **This is one tool that handles all retailers.** Do not split into per-retailer calls — there are no per-retailer tools.

**Input parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `product` | string | ✅ | Product name, model number, or UPC barcode |
| `size` | string | ❌ | Shoe or clothing size — pass separately, never embed in the product string |
| `zip_code` | string | ❌ | US ZIP code for localised pricing (default: `"10001"`) |

**Size handling:**

```json
// ✅ Correct
{ "product": "Nike Air Force 1 Low", "size": "10.5" }

// ❌ Wrong — size embedded in product string
{ "product": "Nike Air Force 1 Low size 10.5" }
```

**Example response:**

```json
{
  "product_name": "Sony WH-1000XM5",
  "match_confidence": "HIGH",
  "retailers_found": 4,
  "search_exhausted": false,
  "amazon":   { "found": true, "price": 278.00, "effective_price": 278.00, "in_stock": true,
                "url": "https://www.amazon.com/dp/...", "title": "Sony WH-1000XM5...",
                "confidence": "HIGH", "coupon_available": false, "coupon_discount": 0,
                "size_match": "N/A", "condition": "new" },
  "walmart":  { "found": true, "price": 294.95, ... },
  "target":   { "found": true, "price": 299.99, ... },
  "best_buy": { "found": true, "price": 289.99, ... },
  "cheapest_retailer": "amazon",
  "cheapest_price": 278.00,
  "price_spread_pct": 7.3,
  "deal_score": 10,
  "verdict": "Amazon is cheapest at $278.00. The current price matches the lowest we've ever observed across 7 price checks (low: $278.00). Strong buying signal. Deal Score: 10/10. ✅ Buy now.",
  "price_context": "Prices are at or near the lowest observed.",
  "disclaimer": "Prices queried from a fixed US location..."
}
```

**Key fields:**

- `size_match` — always `"N/A"` for electronics, `"MATCH"` or `"MISMATCH"` for sized products
- `not_listed: true` — retailer genuinely does not carry this product
- `search_exhausted: true` — read `no_results_advice` and stop; do not retry with query variations

---

### 2. `get_price_history`

Retrieve rolling price history from the local cache.

```json
// Request
{ "product": "Sony WH-1000XM5" }

// Response
{
  "product": "Sony WH-1000XM5",
  "observed_low": 278.00,
  "data_points": 7,
  "history": [
    { "ts": 1774260993, "retailer": "amazon", "price": 278.00 },
    { "ts": 1774260993, "retailer": "target", "price": 299.99 }
  ],
  "message": "Found 7 price observations. Observed low: $278.00."
}
```

> LOW confidence matches are excluded from price history — they may be wrong products and should not set historical lows.

---

### 3. `get_deal_score`

Quick deal score for a known price. No live API calls — cache lookup only, under 500ms.

```json
// Request
{ "product": "Sony WH-1000XM5", "current_price": 278.00 }

// Response
{
  "product": "Sony WH-1000XM5",
  "current_price": 278.00,
  "deal_score": 10,
  "observed_low": 278.00,
  "verdict": "The current price matches the lowest we've ever observed across 7 price checks. ✅ Buy now."
}
```

---

### 4. `check_availability`

Stock availability check across all four retailers.

```json
// Request
{ "product": "Nike Air Force 1 Low", "zip_code": "90210" }

// Response
{
  "product_name": "Nike Air Force 1 Low",
  "amazon":   { "found": true,  "in_stock": true,  "confidence": "HIGH" },
  "walmart":  { "found": true,  "in_stock": true,  "confidence": "HIGH" },
  "target":   { "found": false, "in_stock": false, "confidence": "NOT_FOUND" },
  "best_buy": { "found": false, "in_stock": false, "confidence": "NOT_FOUND" },
  "summary": "In stock at 2 of 4 retailers."
}
```

---

## HTTP Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check — `{"status": "ok"}` |
| `GET` | `/debug` | API key load status — no secrets exposed |
| `POST` | `/mcp` | MCP protocol endpoint — CTX JWT required for `tools/call` |

---

## Integration with LLM Clients

### Claude Desktop (stdio)

```json
{
  "mcpServers": {
    "retailradar": {
      "command": "python",
      "args": ["-m", "server"],
      "cwd": "/path/to/context-mcp",
      "env": {
        "SERPAPI_KEY": "your_key",
        "SCRAPERAPI_KEY": "your_key",
        "REDCIRCLE_KEY": "your_key"
      }
    }
  }
}
```

### HTTP/SSE clients (CTX Protocol, Continue, etc.)

```
https://your-deployed-host/mcp
```

---

## CTX Protocol Authentication

The server implements the CTX Protocol SDK's selective authentication model using `verify_context_request` from the `ctxprotocol` Python package.

| MCP Method | Auth Required | Reason |
|---|---|---|
| `initialize` | ❌ No | Session setup |
| `tools/list` | ❌ No | Discovery — agents must see schemas without paying |
| `resources/list` | ❌ No | Discovery |
| `tools/call` | ✅ **Yes** | Execution — costs money, runs live API queries |

Setting `CTX_AUDIENCE` to your deployment URL additionally scopes JWT verification so tokens issued for other tools cannot be replayed against yours.

---

## Testing

```bash
pytest tests/ -v
```

| Test file | What it covers |
|---|---|
| `tests/test_matcher.py` | Fuzzy scoring, model number normalisation, variant conflict rejection (e.g. XM5 vs XM6 → score 0), size matching, dynamic bundle scoring |
| `tests/test_scorer.py` | Deal score computation, thin-history handling (< 3 observations), verdict generation for all score bands |
| `tests/test_cache.py` | Price history storage, retrieval, rolling minimum, 10-minute response cache TTL |
| `tests/test_server_tools.py` | Integration tests for all four MCP tools (mocked API responses) |

---

## Deployment

### Railway / Render / Fly.io

1. Push the repository to your hosting platform
2. Set `SERPAPI_KEY`, `SCRAPERAPI_KEY`, `REDCIRCLE_KEY`, `CTX_AUDIENCE` in the platform dashboard
3. The `Procfile` is pre-configured:

```
web: uvicorn app:app --host 0.0.0.0 --port $PORT
```

Pin Python to 3.11 for Railway compatibility:

```
# .python-version
3.11.9
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## CTX Protocol Registration

The tool is live on the CTX Protocol marketplace at Tool ID `4bf2fc7a-0863-43f9-95f2-6f3b437ab6aa`.

**Tool pricing:**

| Tool | Response price |
|---|---|
| `compare_prices` | $0.10 |
| `get_price_history` | $0.05 |
| `get_deal_score` | $0.05 |
| `check_availability` | $0.05 |

---

## Project Structure

```
context-mcp/
├── server.py              # MCP server — tool definitions, schemas, orchestration
├── app.py                 # Starlette ASGI app — HTTP transport, CTX auth middleware
├── src/
│   ├── __init__.py
│   ├── matcher.py         # 5-tier product matching engine
│   ├── retailers.py       # Amazon / Walmart / Target / Best Buy async fetchers
│   ├── scorer.py          # Deal score (0–10) and plain-English verdict generator
│   └── cache.py           # Rolling price cache + 10-minute response cache
├── tests/
│   ├── test_matcher.py
│   ├── test_scorer.py
│   ├── test_cache.py
│   └── test_server_tools.py
├── cache/
│   └── price_history.json
├── Procfile
├── requirements.txt       # Includes ctxprotocol, fastembed, numpy, python-dotenv
├── .python-version        # Pins to 3.11.9 for Railway compatibility
├── .env.example
├── .gitignore
└── LICENSE
```

---

## Contributing

1. Fork and create a feature branch from `main`
2. Install dependencies and confirm tests pass: `pytest tests/ -v`
3. Write tests for new functionality
4. Submit a pull request with a clear description

---

## License

MIT
