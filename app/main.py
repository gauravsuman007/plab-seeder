"""HTTP API and web UI."""

import base64
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .engine import Engine
from .pornolab import PornolabError
from .qbit import QbitError
from .store import DB, SettingsStore
from . import parsing as _parsing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HERE = Path(__file__).parent
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

store = SettingsStore()
db = DB()
engine = Engine(store, db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.start()
    db.event("Seeder started")
    yield
    await engine.stop()


app = FastAPI(title="pornolab-seeder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional: set APP_PASSWORD to require it (any username) for the whole UI."""
    if APP_PASSWORD and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, APP_PASSWORD)
            except ValueError:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="pornolab-seeder"'})
    return await call_next(request)


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except ValueError:
        return {}


def _fail(e: Exception, status: int = 400):
    raise HTTPException(status_code=status, detail=str(e))


@app.get("/")
async def index():
    return FileResponse(HERE / "templates" / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/state")
async def state():
    return engine.state()


@app.get("/api/settings")
async def get_settings():
    return store.public()


@app.post("/api/settings")
async def set_settings(request: Request):
    before = (store.settings.qbit_url, store.settings.qbit_username, store.settings.qbit_password,
              store.settings.pornolab_url)
    try:
        store.update(await _body(request))
    except (TypeError, ValueError) as e:
        _fail(e)
    after = (store.settings.qbit_url, store.settings.qbit_username, store.settings.qbit_password,
             store.settings.pornolab_url)
    if before != after:
        engine.reconfigure()
    db.event("Settings saved")
    return store.public()


@app.post("/api/auto")
async def set_auto(request: Request):
    on = bool((await _body(request)).get("enabled"))
    store.update({"auto": on})
    db.event(f"Automatic seeding {'on' if on else 'off'}")
    if on:
        await engine.decide()
    return {"auto": on}


@app.post("/api/guard-override")
async def set_guard_override(request: Request):
    on = bool((await _body(request)).get("enabled"))
    store.update({"guard_override": on})
    db.event(f"Download guard override {'enabled' if on else 'disabled'}", "warning" if on else "info")
    await engine.decide()      # re-rank and, if auto is on, act on it now rather than up to DECIDE_INTERVAL later
    return {"guard_override": on}


@app.post("/api/pornolab/login")
async def pl_login(request: Request):
    b = await _body(request)
    username = (b.get("username") or store.settings.pornolab_username or "").strip()
    password = b.get("password") or store.settings.pornolab_password
    if not username or not password:
        _fail(ValueError("username and password are required"))
    return await engine.login(username, password, bool(b.get("remember", True)), b.get("captcha") or None)


@app.post("/api/pornolab/logout")
async def pl_logout():
    engine.logout()
    return {"ok": True}


@app.post("/api/profile/refresh")
async def profile_refresh():
    try:
        await engine.refresh_profile()
    except PornolabError as e:
        _fail(e, 502)
    return {"ok": True}


@app.post("/api/scan")
async def scan():
    try:
        await engine.scan()
    except PornolabError as e:
        _fail(e, 502)
    return engine.state()["scan"]


@app.post("/api/candidates/{topic_id}/add")
async def add_candidate(topic_id: int):
    try:
        meta = await engine.add(topic_id)
    except (ValueError, PornolabError, QbitError) as e:
        _fail(e)
    return {"ok": True, **meta}


@app.post("/api/torrents/{infohash}/{action}")
async def torrent_action(infohash: str, action: str, request: Request):
    try:
        if action == "stop":
            await engine.qb.stop([infohash])
        elif action == "start":
            await engine.qb.start([infohash])
        elif action == "remove":
            b = await _body(request)
            await engine.remove(infohash, bool(b.get("delete_files", True)))
        else:
            _fail(ValueError(f"unknown action {action}"), 404)
    except QbitError as e:
        _fail(e, 502)
    if action != "remove":      # remove() logs its own event, with the reason
        db.event(f"{action.capitalize()} {infohash[:8]}")
    return {"ok": True}


@app.get("/proxy/ann")
async def announce_proxy(request: Request):
    """Relay a tracker announce, reporting downloaded=0.

    Forwards the raw query string: info_hash/peer_id are binary and must not be
    decoded and re-encoded. The tracker would otherwise see the peer at this
    app's address, so the VPN address qBittorrent reports is passed as ip=.
    """
    import re
    token = request.query_params.get("t", "")
    real = db.meta(f"ann:{token}")
    if not real:
        return Response(b"d14:failure reason9:bad tokene", media_type="text/plain")
    raw = re.sub(r"(^|&)t=[^&]*", "", request.url.query).lstrip("&")
    raw = re.sub(r"(^|&)downloaded=\d+", r"\1downloaded=0", raw)
    ip = (engine.qbit.get("transfer") or {}).get("last_external_address_v4")
    if ip and "&ip=" not in "&" + raw:
        raw += f"&ip={ip}"
    url = real + ("&" if "?" in real else "?") + raw
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
        return Response(content=r.content, media_type="text/plain")
    except Exception as e:
        logging.getLogger("seeder").warning("announce proxy error: %s", type(e).__name__)
        return Response(b"d14:failure reason11:proxy errore", media_type="text/plain")


@app.post("/api/proxy-trackers/{infohash}")
async def proxy_trackers(infohash: str):
    try:
        return {"ok": True, "rewritten": await engine.proxy_existing(infohash)}
    except QbitError as e:
        _fail(e, 502)


_pending_register_form: _parsing.RegisterForm | None = None


@app.get("/api/register/form")
async def register_form():
    """Fetch the PornoLab registration page and return the image captcha + lists."""
    global _pending_register_form
    try:
        form = await engine.pl.register_form()
    except PornolabError as e:
        _fail(e, 502)
    _pending_register_form = form
    # fetch captcha image the same way the login flow does
    img_data = None
    if form.captcha_url:
        try:
            img = await engine.pl.client.get(
                form.captcha_url,
                headers={"Referer": f"{engine.pl.base}/forum/profile.php?mode=register"},
            )
            mime = img.headers.get("content-type", "image/png").split(";")[0]
            if mime.startswith("image/"):
                img_data = f"data:{mime};base64,{base64.b64encode(img.content).decode()}"
        except Exception:
            pass
    return {
        "captcha": img_data,
        "countries": form.countries,
        "timezones": form.timezones,
    }


@app.post("/api/register")
async def register(request: Request):
    global _pending_register_form
    b = await _body(request)
    if not _pending_register_form:
        _fail(ValueError("fetch the registration form first (/api/register/form)"))
    try:
        result = await engine.pl.register(
            username=b.get("username", "").strip(),
            password=b.get("password", ""),
            email=b.get("email", "").strip(),
            captcha_code=b.get("captcha", ""),
            form=_pending_register_form,
            country=str(b.get("country", "0")),
            timezone=str(b.get("timezone", "100")),
        )
    except PornolabError as e:
        _fail(e, 502)
    if result["ok"]:
        _pending_register_form = None
        db.event("Registration submitted – check your email for the activation link")
    return result


@app.get("/api/events")
async def events(limit: int = 200):
    return db.events(min(limit, 1000))


@app.get("/api/history")
async def history(days: int = 30):
    return db.history(time.time() - days * 86400)


@app.get("/api/torrents/history")
async def torrent_history():
    return db.torrents()


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    logging.getLogger("seeder").exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
