from dotenv import load_dotenv
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

import os
import uvicorn
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.types import Receive, Scope, Send
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from server import server

# ── Session manager ───────────────────────────────────────────────────────────
session_manager = StreamableHTTPSessionManager(
    app=server,
    json_response=True,
    stateless=True,
)

# ── ASGI handler — must use Mount, not Route ──────────────────────────────────
# Route expects a Response to be returned. StreamableHTTPSessionManager.handle_request
# is a raw ASGI callable that writes directly to the send channel and returns None.
# Using Route causes "NoneType is not callable". Mount handles raw ASGI apps correctly.
async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
    await session_manager.handle_request(scope, receive, send)

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    async with session_manager.run():
        yield

# ── Regular routes ────────────────────────────────────────────────────────────
async def health_check(request: Request):
    return JSONResponse({"status": "ok", "tool": "price-intelligence-feed"})

async def debug(request: Request):
    serpapi_key    = os.getenv("SERPAPI_KEY",    "")
    scraperapi_key = os.getenv("SCRAPERAPI_KEY", "")
    redcircle_key  = os.getenv("REDCIRCLE_KEY",  "")
    placeholders   = {"your_serpapi_key_here", "your_scraperapi_key_here", "your_redcircle_key_here"}
    return JSONResponse({
        "serpapi_loaded":    bool(serpapi_key)    and serpapi_key    not in placeholders,
        "scraperapi_loaded": bool(scraperapi_key) and scraperapi_key not in placeholders,
        "redcircle_loaded":  bool(redcircle_key)  and redcircle_key  not in placeholders,
        "serpapi_preview":    serpapi_key[:8]    + "..." if serpapi_key    else "EMPTY",
        "scraperapi_preview": scraperapi_key[:8] + "..." if scraperapi_key else "EMPTY",
        "redcircle_preview":  redcircle_key[:8]  + "..." if redcircle_key  else "EMPTY",
    })

# Wrap handle_mcp to also work as a Starlette Route endpoint
# This avoids the 307 redirect that Mount("/mcp") causes when clients hit /mcp without trailing slash
async def mcp_endpoint(request: Request):
    await session_manager.handle_request(request.scope, request.receive, request._send)

app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", endpoint=health_check,  methods=["GET"]),
        Route("/debug",  endpoint=debug,          methods=["GET"]),
        Route("/mcp",    endpoint=mcp_endpoint,   methods=["GET", "POST", "DELETE"]),
        Mount("/mcp",    app=handle_mcp),          # catches /mcp/* subpaths
    ],
)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)