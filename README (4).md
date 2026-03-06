# Multi-Retailer Price Intelligence Feed
**CTX Protocol MCP Tool — Python**

Compares real-time product prices across Amazon, Walmart, and Target. Returns the cheapest retailer, a deal score (0-10), historical price context, and a plain-English buy/wait recommendation.

Replaces: **Keepa Pro** (€19–€459/month) and **Jungle Scout** ($49–$129/month)

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API keys
```bash
cp .env.example .env
# Fill in your API keys in .env
```

**Required API keys:**
| Key | Provider | Free Tier | Purpose |
|-----|----------|-----------|---------|
| `SERPAPI_KEY` | [serpapi.com](https://serpapi.com) | 100 searches/mo | Amazon data |
| `SCRAPERAPI_KEY` | [scraperapi.com](https://scraperapi.com) | 5,000 credits | Walmart data |
| `REDCIRCLE_KEY` | [redcircleapi.com](https://redcircleapi.com) | Free trial | Target data |

### 3. Run the server
```bash
python server.py
```

### 4. Run tests
```bash
pytest tests/ -v
```

---

## Example Query

**Input:**
```json
{
  "product": "Sony WH-1000XM5",
  "zip_code": "10001"
}
```

**Output:**
```json
{
  "product_name": "Sony WH-1000XM5 Wireless Headphones",
  "upc": "027242918368",
  "match_confidence": "HIGH",
  "amazon":  { "price": 279.99, "in_stock": true, "confidence": "HIGH" },
  "walmart": { "price": 289.00, "in_stock": true, "confidence": "HIGH" },
  "target":  { "price": 299.99, "in_stock": true, "confidence": "MEDIUM" },
  "cheapest_retailer": "amazon",
  "cheapest_price": 279.99,
  "price_spread_pct": 6.7,
  "deal_score": 7,
  "verdict": "Amazon is cheapest at $279.99. That's 6.7% cheaper than Target ($299.99). Price is close to the observed low of $249.99 (12.0% above). Good value. Deal Score: 7/10. 👍 Good time to buy.",
  "disclaimer": "Prices queried from a fixed US location (ZIP: 10001)..."
}
```

---

## Project Structure

```
price-intel/
├── server.py          # MCP server entry point + orchestration
├── src/
│   ├── matcher.py     # 4-tier product matching engine (UPC → fuzzy)
│   ├── retailers.py   # Amazon / Walmart / Target fetchers
│   ├── scorer.py      # Deal score (0-10) + verdict generator
│   └── cache.py       # Rolling price minimum cache
├── tests/
│   ├── test_matcher.py
│   ├── test_scorer.py
│   └── test_cache.py
├── cache/
│   └── price_history.json  (auto-created, grows over time)
├── requirements.txt
└── .env.example
```

---

## CTX Protocol Registration

1. Deploy this server to a public HTTPS endpoint (Railway, Render, Fly.io)
2. Register at [ctxprotocol.com/contribute](https://ctxprotocol.com/contribute)
3. Set listing response price to `$0` for initial testing
4. Email `grants@ctxprotocol.com` with 5 test cases for Tier S review

---

## Architecture Notes

- All three retailer calls run **concurrently** via `asyncio.gather()` — total latency ~8s, well within CTX's 30s limit
- Product matching uses a **4-tier waterfall**: UPC → model number → fuzzy title → graceful partial
- Price cache builds a rolling minimum over time, improving deal score accuracy with each query
- All responses include `match_confidence` and per-retailer `confidence` flags — reviewers can always trust the output
