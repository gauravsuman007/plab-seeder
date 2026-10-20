"""The announce-facing app (ANNOUNCE_PORT): the only thing a reverse proxy needs
to expose. /ann is what torrent clients hit; /v1/* is the small key-gated API
that seeders and the Prowlarr sidecar use to register announce URLs.

Nothing else is served here -- no UI, no docs, no admin API."""

import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import core
from .db import DB

FAIL_WINDOW_S = 600
FAIL_LIMIT = 20


def make_app(db: DB, http: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    failures: dict[str, list[float]] = {}

    def remote_ip(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        return xff or (request.client.host if request.client else "")

    def announce_base(request: Request, client: dict) -> str:
        if client["announce_base"]:
            return client["announce_base"]
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        return f"{scheme}://{request.headers.get('host', '')}"

    def authed_client(request: Request) -> dict:
        """The calling client, or a bare 404 (the API doesn't advertise itself)."""
        ip = remote_ip(request)
        now = time.time()
        recent = [t for t in failures.get(ip, []) if now - t < FAIL_WINDOW_S]
        if len(recent) >= FAIL_LIMIT:
            raise HTTPException(429)
        # A request that came through a reverse proxy is the public internet; the
        # registration API is for the private network unless explicitly opened up.
        proxied = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip"))
        client = db.client_by_key(request.headers.get("x-api-key", ""))
        if (proxied and not db.settings()["allow_remote_api"]) or not client or not client["enabled"]:
            failures[ip] = recent + [now]
            raise HTTPException(404)
        return client

    async def body(request: Request) -> dict:
        ctype = request.headers.get("content-type", "")
        raw = await request.body()
        if "json" in ctype:
            import json
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
        from urllib.parse import parse_qsl
        return dict(parse_qsl(raw.decode("utf-8", "replace")))

    @app.get("/ann")
    async def ann(request: Request):
        content = await core.announce(
            db, http, token=request.query_params.get("t", ""), query=request.url.query,
            remote_ip=remote_ip(request), user_agent=request.headers.get("user-agent", ""))
        return Response(content=content, media_type="text/plain")

    @app.post("/v1/register")
    async def register(request: Request, format: str = "json"):
        client = authed_client(request)
        b = await body(request)
        url = str(b.get("url") or "")
        if not url:
            return JSONResponse({"error": "url is required"}, status_code=400)
        truthy = lambda v: v in (True, 1, "1", "true", "on")      # noqa: E731 -- form bodies send strings
        try:
            res = await core.register(
                db, client, url=url, label=str(b.get("label") or "default"),
                announce_base=announce_base(request, client),
                first_download_at=float(b["first_download_at"]) if b.get("first_download_at") else None,
                bootstrap=truthy(b.get("bootstrap")), passthrough=truthy(b.get("passthrough")))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if format == "text":                       # for the C sidecar: no JSON parsing needed
            return PlainTextResponse(res["announce_url"])
        return res

    @app.post("/v1/identity")
    async def identity(request: Request):
        """Lets a seeder tell the proxy which public IP its torrent client is
        reachable on (e.g. a VPN exit address), sent to the tracker as ip=."""
        client = authed_client(request)
        b = await body(request)
        ident = db.identity(client["id"], str(b.get("label") or "default").strip()[:80])
        if "ip" in b:
            db.execute("update identities set ip=? where id=?", (str(b["ip"]).strip()[:64], ident["id"]))
        return {"ok": True}

    @app.get("/v1/identity")
    async def identity_info(request: Request, label: str = "default"):
        """The proxy owns an identity's fake-upload settings; seeders read them
        (e.g. to decide whether a new account needs a first download)."""
        client = authed_client(request)
        i = db.identity(client["id"], label.strip()[:80])
        return {"label": i["label"], "ip": i["ip"], "fake_upload": bool(i["fake_upload"]),
                "fake_mb_per_hour": i["fake_mb_per_hour"], "first_download_at": i["first_download_at"]}

    @app.get("/v1/whoami")
    async def whoami(request: Request):
        client = authed_client(request)
        return {"client": client["name"], "kind": client["kind"], "announce_base": announce_base(request, client)}

    return app
