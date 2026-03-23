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
from ctxprotocol import verify_context_request, is_protected_mcp_method, ContextError

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
    """
    MCP request handler with CTX Protocol authentication.
    - tools/call requires a valid CTX JWT (protects paid execution)
    - tools/list, initialize etc. pass through freely (discovery)
    """
    if scope["type"] != "http":
        await session_manager.handle_request(scope, receive, send)
        return

    # Read the request body to inspect the MCP method
    body_chunks = []
    more_body = True
    while more_body:
        message = await receive()
        body_chunks.append(message.get("body", b""))
        more_body = message.get("more_body", False)
    raw_body = b"".join(body_chunks)

    # Parse JSON to get MCP method name
    import json as _json
    try:
        mcp_body = _json.loads(raw_body) if raw_body else {}
    except Exception:
        mcp_body = {}

    method = mcp_body.get("method", "")

    # Only verify auth for protected methods (tools/call)
    if is_protected_mcp_method(method):
        # Extract Authorization header from scope
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8")

        try:
            await verify_context_request(
                authorization_header=auth_header,
                audience=os.getenv("CTX_AUDIENCE", ""),
            )
        except ContextError:
            # Return 401 Unauthorized
            response_body = _json.dumps({
                "jsonrpc": "2.0",
                "error": {"code": -32001, "message": "Unauthorized — valid CTX token required"},
                "id": mcp_body.get("id"),
            }).encode()
            await _send_response(scope, send, 401, response_body)
            return

    # Auth passed (or not required) — replay body to session manager
    async def patched_receive():
        return {"type": "http.request", "body": raw_body, "more_body": False}

    await session_manager.handle_request(scope, patched_receive, send)


async def _send_response(scope: Scope, send, status: int, body: bytes):
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [[b"content-type", b"application/json"]],
    })
    await send({"type": "http.response.body", "body": body})

app = MCPRouter(mcp_handler=handle_mcp, fallback=inner_app)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)