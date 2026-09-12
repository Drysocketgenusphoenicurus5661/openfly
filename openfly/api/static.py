"""Serve the built React app from frontend/dist with an SPA fallback, or a plain page explaining how to build it."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from openfly.api.context import error

router = APIRouter(include_in_schema=False)

PLACEHOLDER = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>OpenFly API</title>
<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;line-height:1.5;color:#222}
code,pre{background:#f3f3f3;padding:.15rem .35rem;border-radius:3px}</style></head>
<body>
<h1>OpenFly API is running</h1>
<p>The web UI has not been built yet, so only the API is served. The API lives under
<code>/api</code> (for example <a href="/api/status">/api/status</a>) and the event stream at
<code>/api/events</code>.</p>
<p>To build the UI once (Node 20 or newer):</p>
<pre>cd frontend
pnpm install   (or: npm install)
pnpm build     (or: npm run build)</pre>
<p>Then restart with <code>uv run app.py</code>. During development, <code>pnpm dev</code> in
<code>frontend/</code> serves the UI at <a href="http://localhost:5173">http://localhost:5173</a>
and proxies to this API.</p>
</body></html>
"""


def _dist(request: Request) -> Path:
    return request.app.state.ctx.paths.frontend_dist


@router.get("/")
async def index(request: Request):
    dist = _dist(request)
    page = dist / "index.html"
    if page.is_file():
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return HTMLResponse(PLACEHOLDER)


@router.get("/{path:path}")
async def spa(path: str, request: Request):
    if path == "api" or path.startswith("api/"):
        raise error(404, f"no API route /{path}")
    dist = _dist(request)
    if not (dist / "index.html").is_file():
        return HTMLResponse(PLACEHOLDER)
    root = dist.resolve()
    candidate = (dist / path).resolve()
    if path and candidate.is_file() and root in candidate.parents:
        headers = {"Cache-Control": "public, max-age=31536000, immutable"} if "/assets/" in candidate.as_posix() else {"Cache-Control": "no-cache"}
        return FileResponse(candidate, headers=headers)
    return FileResponse(dist / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})
