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
12. [Testing](#testing)
13. [Deployment](#deployment)
14. [CTX Protocol Registration](#ctx-protocol-registration)
15. [Project Structure](#project-structure)
16. [Contributing](#contributing)
17. [License](#license)

---

## Overview

RetailRadar is an MCP server built with Python that enables large language models and AI assistants to perform real-time product price comparisons across the four largest US consumer retailers. It normalises product names, resolves identifiers, tracks price history, and produces a structured response with actionable recommendations — all within a single tool call.

**Live deployment:** Actively serving queries on the [CTX Protocol marketplace](https://ctxprotocol.com).  
**Response time:** 12–15 seconds for a full four-retailer comparison (within the CTX Protocol 30-second limit).  
**Tool ID:** `4bf2fc7a-0863-43f9-95f2-6f3b437ab6aa`

---

## Features

- **Real-time cross-retailer price comparison** across Amazon, Walmart, Target, and Best Buy
- **5-tier product matching waterfall:** UPC/barcode → model number extraction → semantic embeddings → fuzzy title matching → graceful partial result
- **Smart fuzzy scoring** — normalises model number hyphens (e.g. `WH-1000XM5` and `WH1000XM5` score identically), uses `max(token_sort, partial, token_set)` ratios to handle short queries against long retailer titles, and applies model-number conflict penalties to prevent variant mismatches (XM5 vs XM4)
- **Size-aware matching** for footwear and clothing — validates size from retailer titles and defensively extracts size when passed inline in the product string
- **Deal scoring (0–10)** based on live prices vs. historical rolling minimums, with honest thin-history handling (requires ≥3 observations before drawing conclusions)
- **Plain-English verdicts** — buy-now-or-wait recommendations with supporting price context
- **Coupon detection** — extracts and computes effective prices after coupons and sale badges
- **Availability checking** across all four retailers per ZIP code
- **Rolling price history cache** — improves deal score accuracy over time with zero external dependencies
- **Per-retailer confidence levels** (`HIGH` / `MEDIUM` / `LOW` / `NOT_FOUND`) — every data point is auditable
- **Fully async architecture** — all four retailer fetches run concurrently via `asyncio.gather()` with individual `asyncio.wait_for()` timeouts so a slow retailer never blocks the others
- **Typed response schema** — all fields always present with typed defaults; no null values that could confuse downstream AI agents
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
│   MCPRouter → /mcp  ──► StreamableHTTPSessionManager               │
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
│  product         Walmart /       score + plain-   minimum; JSON      │
│  matching)       Target /        English verdict) file store)        │
│                  Best Buy)                                           │
└──────────────────────────────────────────────────────────────────────┘
```

**Data providers:**

| Retailer | Provider | Notes |
|---|---|---|
| Amazon | SerpApi (`engine=amazon`) | Organic results only — sponsored listings filtered |
| Best Buy | SerpApi (`engine=best_buy`) | Same API key as Amazon |
| Walmart | ScraperAPI (structured endpoint) | 6s hard timeout — fastest to fail gracefully |
| Target | RedCircle API | ZIP-code localised pricing |

---

## Product Matching Pipeline

| Tier | Method | Detail |
|---|---|---|
| 1 | UPC/barcode lookup | Via UPCItemdb API — exact product identity |
| 2 | Model number extraction | Regex extraction of alphanumeric model codes |
| 3 | Semantic similarity | `fastembed` BAAI/bge-small-en-v1.5 — rejects category mismatches (shoes vs earbuds) |
| 4 | Smart fuzzy scoring | RapidFuzz with model-number normalisation and conflict detection |
| 5 | Graceful partial result | Returns best available match with `LOW` confidence |

**Fuzzy scoring details:**  
Model numbers are normalised before comparison so `WH-1000XM5` and `WH1000XM5` always score as identical. The scorer takes `max(token_sort_ratio, partial_ratio, token_set_ratio)` to handle queries shorter than retailer titles. A model-number conflict penalty caps scores at `LOW` when tokens share a root but differ in suffix (e.g. XM5 vs XM4, G10 vs G9).

---

## Prerequisites

- Python 3.10+
- API keys for the four data providers (free tiers available — see [Configuration](#configuration))

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/DrUkachi/context-mcp.git
cd context-mcp

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Configuration

### API Keys

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Provider | Free Tier | Purpose |
|---|---|---|---|
| `SERPAPI_KEY` | serpapi.com | 100 searches/month | Amazon & Best Buy data |
| `SCRAPERAPI_KEY` | scraperapi.com | 5,000 credits/month | Walmart data |
| `REDCIRCLE_KEY` | redcircleapi.com | Free trial | Target data |
| `PORT` | — | — | HTTP server port (default: `8000`) |

Example `.env`:

```
SERPAPI_KEY=your_serpapi_key_here
SCRAPERAPI_KEY=your_scraperapi_key_here
REDCIRCLE_KEY=your_redcircle_key_here
PORT=8000
```

> **Note:** Never commit your `.env` file. API keys should be set as environment variables in your deployment platform (Railway, Render, etc.), not stored in the repository.

### Verify Configuration

```bash
python debug_env.py
```

Sample output:

```json
{
  "serpapi_loaded": true,
  "scraperapi_loaded": true,
  "redcircle_loaded": true,
  "serpapi_preview": "abc12345...",
  "scraperapi_preview": "def67890...",
  "redcircle_preview": "xyz11111..."
}
```

---

## Running the Server

### HTTP Mode (recommended for deployments and CTX Protocol)

```bash
python app.py
# Listening on http://0.0.0.0:8000
```

Or with Uvicorn directly:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

### stdio Mode (for local LLM clients such as Claude Desktop)

```bash
python -m server
```

---

## MCP Tools Reference

The server exposes four MCP tools. All inputs and outputs are JSON. All response fields are always present — no null values.

---

### 1. `compare_prices`

Full cross-retailer price comparison with deal scoring. Queries all four retailers concurrently.

**Input parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `product` | string | ✅ | Product name, model number, or UPC barcode |
| `size` | string | ❌ | Shoe or clothing size — pass separately, e.g. `"10.5"`, `"XL"`. Do NOT include in the product string. |
| `zip_code` | string | ❌ | US ZIP code for localised pricing (default: `"10001"`) |

**Example request:**

```json
{
  "product": "Sony WH-1000XM5",
  "zip_code": "10001"
}
```

**Example response:**

```json
{
  "product_name": "Sony WH-1000XM5 Wireless Headphones",
  "match_confidence": "HIGH",
  "retailers_found": 2,
  "search_exhausted": false,
  "no_results_reason": "",
  "no_results_advice": "",
  "amazon": {
    "found": true,
    "not_listed": false,
    "price": 279.99,
    "effective_price": 279.99,
    "in_stock": true,
    "url": "https://www.amazon.com/dp/B09XS7JWHH",
    "title": "Sony WH-1000XM5 Wireless Noise Canceling Headphones",
    "confidence": "HIGH",
    "coupon_available": false,
    "coupon_text": "",
    "coupon_discount": 0,
    "size_match": false,
    "condition": "new"
  },
  "walmart": {
    "found": true,
    "not_listed": false,
    "price": 289.00,
    "effective_price": 289.00,
    "in_stock": true,
    "url": "https://www.walmart.com/ip/...",
    "title": "Sony WH-1000XM5 Noise Canceling Bluetooth Headphones",
    "confidence": "HIGH",
    "coupon_available": false,
    "coupon_text": "",
    "coupon_discount": 0,
    "size_match": false,
    "condition": "new"
  },
  "target": {
    "found": false,
    "not_listed": true,
    "price": 0,
    "effective_price": 0,
    "in_stock": false,
    "url": "",
    "title": "",
    "confidence": "NOT_FOUND",
    "coupon_available": false,
    "coupon_text": "",
    "coupon_discount": 0,
    "size_match": false,
    "condition": ""
  },
  "best_buy": {
    "found": false,
    "not_listed": true,
    "price": 0,
    "effective_price": 0,
    "in_stock": false,
    "url": "",
    "title": "",
    "confidence": "NOT_FOUND",
    "coupon_available": false,
    "coupon_text": "",
    "coupon_discount": 0,
    "size_match": false,
    "condition": ""
  },
  "cheapest_retailer": "amazon",
  "cheapest_price": 279.99,
  "price_spread_pct": 3.2,
  "deal_score": 7,
  "verdict": "Amazon is cheapest at $279.99. Price is close to the observed low of $249.99 (12.0% above). Good value. Deal Score: 7/10. 👍 Good time to buy.",
  "price_context": "",
  "disclaimer": "Prices queried from a fixed US location. Final prices may vary by region, membership status (Walmart+, Target Circle), and real-time availability. Verify before purchasing."
}
```

> **Retailer fields:** Always present as objects — never null. Check `found: true/false` before reading `price` or `url`. When `not_listed: true`, all numeric fields are `0` and string fields are `""` — this is correct behaviour, not an error.

---

### 2. `get_price_history`

Retrieve rolling price history for a product from the local cache.

**Input parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `product` | string | ✅ | Product name (must match a previously queried product) |

**Example request:**

```json
{ "product": "Sony WH-1000XM5" }
```

**Example response:**

```json
{
  "product": "Sony WH-1000XM5",
  "observed_low": 249.99,
  "data_points": 12,
  "history": [
    { "ts": 1773770186, "retailer": "amazon",  "price": 279.99 },
    { "ts": 1773762043, "retailer": "walmart", "price": 289.00 }
  ],
  "message": "Found 12 price observations for 'Sony WH-1000XM5'. Observed low: $249.99."
}
```

> **Note:** History only exists for products previously queried via `compare_prices`. The deal scorer requires ≥3 observations before drawing historical conclusions.

---

### 3. `get_deal_score`

Quick deal score for a known price — lightweight, no live API calls, cache lookup only. Response time under 500ms.

**Input parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `product` | string | ✅ | Product name |
| `current_price` | number | ✅ | Price to evaluate against historical data |

**Example request:**

```json
{
  "product": "Sony WH-1000XM5",
  "current_price": 249.99
}
```

**Example response:**

```json
{
  "product": "Sony WH-1000XM5",
  "current_price": 249.99,
  "deal_score": 10,
  "observed_low": 249.99,
  "verdict": "The current price matches the lowest we've ever observed across 12 price checks (low: $249.99). Strong buying signal. Deal Score: 10/10. ✅ Buy now — this is a strong deal."
}
```

---

### 4. `check_availability`

Stock availability check across all four retailers.

**Input parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `product` | string | ✅ | Product name, model number, or UPC |
| `zip_code` | string | ❌ | US ZIP code (default: `"10001"`) |

**Example request:**

```json
{
  "product": "Nike Air Force 1 Low",
  "zip_code": "90210"
}
```

**Example response:**

```json
{
  "product_name": "Nike Air Force 1 Low",
  "amazon":   { "found": true,  "in_stock": true,  "url": "https://amazon.com/...",  "confidence": "HIGH" },
  "walmart":  { "found": true,  "in_stock": true,  "url": "https://walmart.com/...", "confidence": "HIGH" },
  "target":   { "found": false, "in_stock": false, "url": "",                         "confidence": "NOT_FOUND" },
  "best_buy": { "found": false, "in_stock": false, "url": "",                         "confidence": "NOT_FOUND" },
  "summary": "Amazon: ✅ In Stock | Walmart: ✅ In Stock | Target: Not found | Best Buy: Not found — In stock at 2 of 4 retailers."
}
```

---

## HTTP Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check — returns `{"status": "ok"}` |
| `GET` | `/debug` | API key status — confirms which keys are loaded (no secrets exposed) |
| `POST` | `/mcp` | MCP protocol endpoint used by LLM clients |

```bash
curl http://localhost:8000/health
curl http://localhost:8000/debug
```

---

## Integration with LLM Clients

### Claude Desktop (stdio)

Add the following to your `claude_desktop_config.json`:

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

Point your MCP client at:

```
https://your-deployed-host/mcp
```

---

## Testing

The test suite uses `pytest` with `pytest-asyncio`. All API calls are mocked — no live credentials required.

```bash
# Run all tests
pytest tests/ -v

# Run a specific module
pytest tests/test_matcher.py -v

# Run a specific test
pytest tests/test_server_tools.py::TestHandleComparePrices::test_returns_all_required_fields -v
```

| Test file | What it covers |
|---|---|
| `tests/test_matcher.py` | Fuzzy scoring, model number normalisation, variant conflict detection, size matching |
| `tests/test_scorer.py` | Deal score computation, thin-history handling, verdict generation for all score bands |
| `tests/test_cache.py` | Price history storage, retrieval, rolling minimum computation |
| `tests/test_server_tools.py` | Integration tests for all four MCP tools (mocked API responses) |

---

## Deployment

The server is a standard ASGI application deployable anywhere that supports Python.

### Railway / Render / Fly.io

1. Push the repository to your hosting platform
2. Set the required environment variables (`SERPAPI_KEY`, `SCRAPERAPI_KEY`, `REDCIRCLE_KEY`) in the platform dashboard
3. The `Procfile` is already configured:

```
web: uvicorn app:app --host 0.0.0.0 --port $PORT
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

```bash
docker build -t retailradar .
docker run -p 8000:8000 \
  -e SERPAPI_KEY=your_key \
  -e SCRAPERAPI_KEY=your_key \
  -e REDCIRCLE_KEY=your_key \
  retailradar
```

---

## CTX Protocol Registration

The tool is live on the CTX Protocol marketplace. To register your own deployment:

1. Deploy to a public HTTPS endpoint
2. Register at [ctxprotocol.com/contribute](https://ctxprotocol.com/contribute)
3. Set listing response prices as below
4. Email `grants@ctxprotocol.com` with five representative test cases for Tier S review

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
├── app.py                 # Starlette ASGI app — HTTP transport & session management
├── src/
│   ├── __init__.py
│   ├── matcher.py         # 5-tier product matching engine with smart fuzzy scoring
│   ├── retailers.py       # Amazon / Walmart / Target / Best Buy async fetchers
│   ├── scorer.py          # Deal score (0–10) and plain-English verdict generator
│   └── cache.py           # Rolling price minimum cache (JSON file store)
├── tests/
│   ├── __init__.py
│   ├── test_matcher.py
│   ├── test_scorer.py
│   ├── test_cache.py
│   └── test_server_tools.py
├── cache/
│   └── price_history.json # Auto-created on first query; grows with each comparison
├── conftest.py            # Pytest configuration
├── pytest.ini             # asyncio_mode = auto
├── Procfile               # Railway / Heroku deployment
├── debug_env.py           # Utility to verify .env is loaded correctly
├── requirements.txt       # Python dependencies
├── .env.example           # API key template (safe to commit)
├── .gitignore
└── LICENSE                # MIT
```

---

## Contributing

1. Fork the repository and create a feature branch from `main`
2. Install dependencies and confirm tests pass: `pytest tests/ -v`
3. Write tests for any new functionality
4. Submit a pull request with a clear description of the change

---
