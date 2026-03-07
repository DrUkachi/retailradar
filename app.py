from dotenv import load_dotenv
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

import os
import uvicorn
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, RedirectResponse
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

# ── Build inner Starlette app (health + debug only) ───────────────────────────
inner_app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", endpoint=health_check, methods=["GET"]),
        Route("/debug",  endpoint=debug,         methods=["GET"]),
    ],
)

# ── Outer ASGI wrapper — intercepts /mcp before Starlette sees it ─────────────
# This avoids the Route vs Mount NoneType conflict entirely.
# Any path starting with /mcp goes straight to the session manager.
# Everything else falls through to the Starlette app.
class MCPRouter:
    def __init__(self, mcp_handler, fallback):
        self.mcp_handler = mcp_handler
        self.fallback     = fallback

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                await self.mcp_handler(scope, receive, send)
                return
        await self.fallback(scope, receive, send)

async def handle_mcp(scope: Scope, receive: Receive, send: Send):
    await session_manager.handle_request(scope, receive, send)

app = MCPRouter(mcp_handler=handle_mcp, fallback=inner_app)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)