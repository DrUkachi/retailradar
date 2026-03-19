# Multi-Retailer Price Intelligence Feed

> A Python MCP (Model Context Protocol) server that compares real-time product prices across **Amazon, Walmart, Target, and Best Buy** — returning the cheapest retailer, a deal score (0–10), historical price context, and a plain-English buy/wait recommendation.

**Replaces:** Keepa Pro (€19–€459/month) · Jungle Scout ($49–$129/month)

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Server](#running-the-server)
- [MCP Tools Reference](#mcp-tools-reference)
- [HTTP Endpoints](#http-endpoints)
- [Integration with LLM Clients](#integration-with-llm-clients)
- [Testing](#testing)
- [Deployment](#deployment)
- [CTX Protocol Registration](#ctx-protocol-registration)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

The **Multi-Retailer Price Intelligence Feed** is an MCP server built with Python that enables large language models (LLMs) and AI assistants to perform real-time product price comparisons across the four largest US retailers. It normalizes product names, resolves identifiers, tracks price history, and produces a structured response with actionable recommendations — all within a single tool call.

**Response time:** ~8 seconds for a full four-retailer comparison (well within the CTX Protocol 30-second limit).

---

## Features

- **Real-time cross-retailer price comparison** across Amazon, Walmart, Target, and Best Buy
- **5-tier product matching waterfall:** UPC/barcode → model number extraction → semantic embeddings → fuzzy title matching → graceful partial result
- **Deal scoring (0–10)** based on live prices vs. historical rolling minimums
- **Plain-English verdicts:** buy-now-or-wait recommendations with supporting context
- **Coupon detection and extraction** — computes effective prices after discounts
- **Availability checking** across all four retailers per ZIP code
- **Rolling price history cache** — improves accuracy over time with zero external dependencies
- **Per-retailer confidence levels** (HIGH / MEDIUM / LOW / NOT_FOUND) — every data point is auditable
- **Fully async architecture** — all retailer fetches run concurrently via `asyncio.gather()`
- **Dual transport support** — HTTP/SSE (for web deployments) and stdio (for local LLM clients)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         LLM / AI Client                             │
│           (Claude Desktop, Continue, CTX Protocol, etc.)           │
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
│  ┌──────────────┐  ┌────────────────┐  ┌───────────┐  ┌──────────┐ │
│  │compare_prices│  │get_price_history│  │get_deal   │  │check_    │ │
│  │              │  │                │  │_score     │  │availabil-│ │
│  └──────┬───────┘  └────────┬───────┘  └─────┬─────┘  │ity       │ │
│         │                   │                │         └────┬─────┘ │
└─────────┼───────────────────┼────────────────┼──────────────┼───────┘
          │                   │                │              │
┌─────────▼───────────────────▼────────────────▼──────────────▼───────┐
│                          src/  (Core Modules)                        │
│                                                                      │
│  matcher.py     retailers.py    scorer.py       cache.py            │
│  (5-tier        (Amazon /       (0–10 deal      (rolling price      │
│  product        Walmart /       score + plain-  minimum; JSON       │
│  matching)      Target /        English verdict) file store)        │
│                 Best Buy)                                            │
└──────────────────────────────────────────────────────────────────────┘
```

### Product Matching — 5-Tier Waterfall

| Tier | Method | Notes |
|------|--------|-------|
| 1 | **UPC/barcode lookup** | Via UPCItemdb API |
| 2 | **Model number extraction** | Regex patterns |
| 3 | **Semantic similarity** | fastembed `BAAI/bge-small-en-v1.5` + cosine similarity |
| 4 | **Fuzzy title matching** | RapidFuzz — handles typos and word-order differences |
| 5 | **Graceful partial result** | Returns best available match with LOW confidence |

---

## Prerequisites

- **Python 3.10+**
- API keys for the four data providers (free tiers available — see [Configuration](#configuration))

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/DrUkachi/context-mcp.git
cd context-mcp

# 2. (Recommended) Create and activate a virtual environment
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

Open `.env` and set the following variables:

| Variable | Provider | Free Tier | Purpose |
|----------|----------|-----------|---------|
| `SERPAPI_KEY` | [serpapi.com](https://serpapi.com) | 100 searches/month | Amazon & Best Buy data |
| `SCRAPERAPI_KEY` | [scraperapi.com](https://scraperapi.com) | 5,000 credits | Walmart data |
| `REDCIRCLE_KEY` | [redcircleapi.com](https://redcircleapi.com) | Free trial | Target data |
| `PORT` | — | — | HTTP server port (default: `8000`) |

**Example `.env`:**

```dotenv
SERPAPI_KEY=your_serpapi_key_here
SCRAPERAPI_KEY=your_scraperapi_key_here
REDCIRCLE_KEY=your_redcircle_key_here
PORT=8000
```

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

The server exposes four MCP tools. All inputs and outputs are JSON.

---

### 1. `compare_prices`

Full cross-retailer price comparison with deal scoring.

**Input parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `product` | string | ✅ | Product name, model number, or UPC/barcode |
| `size` | string | ❌ | Size for clothing or footwear (e.g., `"10.5"`, `"XL"`) |
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
  "upc": "027242918368",
  "match_confidence": "HIGH",
  "amazon":   { "price": 279.99, "in_stock": true,  "confidence": "HIGH",   "coupon": null },
  "walmart":  { "price": 289.00, "in_stock": true,  "confidence": "HIGH",   "coupon": null },
  "target":   { "price": 299.99, "in_stock": true,  "confidence": "MEDIUM", "coupon": null },
  "best_buy": { "price": 319.99, "in_stock": false, "confidence": "HIGH",   "coupon": null },
  "cheapest_retailer": "amazon",
  "cheapest_price": 279.99,
  "price_spread_pct": 12.8,
  "deal_score": 7,
  "verdict": "Amazon is cheapest at $279.99. 12.8% cheaper than Best Buy. Price is 12% above observed low ($249.99). Good value. Deal Score: 7/10. 👍 Good time to buy.",
  "disclaimer": "Prices queried from a fixed US location (ZIP: 10001). Actual prices may vary by region."
}
```

---

### 2. `get_price_history`

Retrieve rolling price history for a product from the local cache.

**Input parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `product` | string | ✅ | Product name |

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
    { "timestamp": "2026-03-15T10:22:00Z", "retailer": "amazon",  "price": 279.99 },
    { "timestamp": "2026-03-10T08:05:00Z", "retailer": "walmart", "price": 289.00 }
  ],
  "message": "12 price observations on record."
}
```

---

### 3. `get_deal_score`

Quick deal score for a known price — lightweight, no API calls, cache lookup only. Response time under 500 ms.

**Input parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `product` | string | ✅ | Product name |
| `current_price` | number | ✅ | Price to evaluate |

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
  "verdict": "Excellent deal! This matches the all-time observed low ($249.99). Buy now. Deal Score: 10/10."
}
```

---

### 4. `check_availability`

Stock availability check across all four retailers.

**Input parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
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
  "product_name": "Nike Air Force 1 Low White",
  "amazon":   { "found": true,  "in_stock": true,  "url": "https://amazon.com/...",   "confidence": "HIGH" },
  "walmart":  { "found": true,  "in_stock": true,  "url": "https://walmart.com/...",  "confidence": "HIGH" },
  "target":   { "found": true,  "in_stock": false, "url": "https://target.com/...",   "confidence": "HIGH" },
  "best_buy": { "found": false, "in_stock": false, "url": "",                          "confidence": "NOT_FOUND" },
  "summary": "Amazon: ✅ In Stock | Walmart: ✅ In Stock | Target: ❌ Out of Stock | Best Buy: Not found — In stock at 2 of 4 retailers."
}
```

---

## HTTP Endpoints

When running in HTTP mode, the following utility endpoints are available:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check — returns `{"status": "ok", "tool": "price-intelligence-feed"}` |
| `GET` | `/debug` | API key status — shows which keys are loaded (no secrets exposed) |
| `POST` | `/mcp` | MCP protocol endpoint (used by LLM clients) |

```bash
# Health check
curl http://localhost:8000/health

# API key status
curl http://localhost:8000/debug
```

---

## Integration with LLM Clients

### Claude Desktop (stdio)

Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "price-intelligence": {
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
http://your-server-host:8000/mcp
```

---

## Testing

The test suite uses **pytest** with **pytest-asyncio**. All API calls are mocked — no live credentials are needed.

```bash
# Run all tests with verbose output
pytest tests/ -v

# Run a specific test module
pytest tests/test_scorer.py -v

# Run a specific test
pytest tests/test_server_tools.py::TestHandleComparePrices::test_returns_all_required_fields -v
```

### Test coverage

| Test file | What it covers |
|-----------|---------------|
| `tests/test_matcher.py` | Fuzzy matching, variant extraction, title normalisation |
| `tests/test_scorer.py` | Deal score computation, verdict generation for all score bands |
| `tests/test_cache.py` | Price history storage, retrieval, rolling minimum computation |
| `tests/test_server_tools.py` | Integration tests for all four MCP tools (mocked API responses) |

---

## Deployment

The server is a standard ASGI application and can be deployed anywhere that supports Python.

### Railway / Render / Fly.io

1. Push the repository to your hosting platform.
2. Set the required environment variables (`SERPAPI_KEY`, `SCRAPERAPI_KEY`, `REDCIRCLE_KEY`) in the platform dashboard.
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
docker build -t price-intelligence-feed .
docker run -p 8000:8000 \
  -e SERPAPI_KEY=your_key \
  -e SCRAPERAPI_KEY=your_key \
  -e REDCIRCLE_KEY=your_key \
  price-intelligence-feed
```

---

## CTX Protocol Registration

Once the server is deployed to a public HTTPS endpoint, register it with the CTX Protocol to make it discoverable by the Context app:

1. Deploy to a public HTTPS endpoint (Railway, Render, Fly.io, etc.)
2. Register at [ctxprotocol.com/contribute](https://ctxprotocol.com/contribute)
3. Set the listing response price to `$0` for initial testing
4. Email `grants@ctxprotocol.com` with five representative test cases for Tier S review

**Pricing configuration** (set in `server.py`):

| Tool | Response price |
|------|---------------|
| `compare_prices` | $0.10 |
| `get_price_history` | $0.05 |
| `get_deal_score` | $0.05 |
| `check_availability` | $0.05 |

---

## Project Structure

```
context-mcp/
├── server.py              # MCP server entry point — tool definitions & orchestration
├── app.py                 # Starlette ASGI app — HTTP transport & session management
├── src/
│   ├── __init__.py
│   ├── matcher.py         # 5-tier product matching engine
│   ├── retailers.py       # Amazon / Walmart / Target / Best Buy API fetchers
│   ├── scorer.py          # Deal score (0–10) and verdict generator
│   └── cache.py           # Rolling price minimum cache (JSON file)
├── tests/
│   ├── __init__.py
│   ├── test_matcher.py
│   ├── test_scorer.py
│   ├── test_cache.py
│   └── test_server_tools.py
├── cache/
│   └── price_history.json # Auto-created; grows with each query
├── conftest.py            # Pytest configuration (Windows path compatibility)
├── pytest.ini             # asyncio_mode = auto
├── Procfile               # Railway / Heroku deployment
├── debug_env.py           # Utility to verify .env is loaded correctly
├── requirements.txt       # Python dependencies
├── .env.example           # API key template
├── .gitignore
└── LICENSE                # MIT
```

---

## Contributing

1. Fork the repository and create your feature branch from `main`.
2. Install dependencies and ensure tests pass: `pytest tests/ -v`
3. Write tests for any new functionality.
4. Submit a pull request with a clear description of the change.

---

## License

[MIT](LICENSE) — Copyright © 2026 Ukachi Osisiogu
