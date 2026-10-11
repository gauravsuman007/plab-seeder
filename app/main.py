"""HTTP API and web UI.

Multi-account: one Engine per PornoLab account, held in the `engines`
registry keyed by account_id. Nearly every route takes an `account_id`
(query param on GETs, body field on POSTs) and resolves to that account's
Engine via `_engine(account_id)`. `account_id` defaults to 1 for backward
compatibility with the single-account deployment this app started as —
`migrate_legacy_single_account` guarantees account 1 exists on first boot
after an upgrade.
"""

import base64
import logging
import math
import os
import random
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .engine import Engine
from .pornolab import PornolabError
from .qbit import QbitError
from .store import (
    DB, GlobalSettingsStore, AccountStore, AccountSettingsHandle, AccountDB,
    AccountSettings, migrate_legacy_single_account,
)
from . import tailscale as _ts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("seeder")

HERE = Path(__file__).parent
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

global_store = GlobalSettingsStore()
db = DB()
account_store = AccountStore(db)
_legacy_account_id = migrate_legacy_single_account(db, global_store)

# The pre-multi-account cookie file lived at a fixed path; carry it over to
# account 1's new per-account path so an upgrading deployment doesn't lose
# its live PornoLab session and have to log in again.
from .store import DATA_DIR as _DATA_DIR
_legacy_cookies = _DATA_DIR / "pornolab_cookies.json"
_new_cookies = _DATA_DIR / f"pornolab_cookies_{_legacy_account_id}.json"
if _legacy_cookies.exists() and not _new_cookies.exists():
    _new_cookies.write_bytes(_legacy_cookies.read_bytes())
    _new_cookies.chmod(0o600)

engines: dict[int, Engine] = {}


def _cookies_path(account_id: int) -> Path:
    from .store import DATA_DIR
    return DATA_DIR / f"pornolab_cookies_{account_id}.json"


def _build_engine(account_id: int) -> Engine:
    handle = AccountSettingsHandle(global_store, account_store, account_id)
    adb = AccountDB(db, account_id)
    return Engine(handle, adb, _cookies_path(account_id))


def _engine(account_id: int) -> Engine:
    eng = engines.get(account_id)
    if not eng:
        _fail(ValueError(f"unknown account_id {account_id}"), 404)
    return eng


@asynccontextmanager
async def lifespan(app: FastAPI):
    for row in db.list_accounts():
        eng = _build_engine(row["id"])
        engines[row["id"]] = eng
        eng.start()
    db.event(next(iter(engines)), "Seeder started") if engines else None
    yield
    for eng in engines.values():
        await eng.stop()


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


# -------------------------------------------------------------------------- #
# accounts
# -------------------------------------------------------------------------- #
@app.get("/api/accounts")
async def list_accounts():
    out = []
    for row in db.list_accounts():
        eng = engines.get(row["id"])
        out.append({
            "id": row["id"], "label": row["label"],
            "pornolab_username": account_store.get_settings(row["id"]).get("pornolab_username"),
            "email_address": row["email_address"], "email_provider": row["email_provider"],
            "logged_in": bool(eng and eng.logged_in),
            "first_download_at": row["first_download_at"],
        })
    return out


@app.get("/api/accounts/{account_id}/credentials")
async def account_credentials(account_id: int):
    """The one place both the email and PornoLab credentials for an account
    are visible together, for the owner to copy/reuse."""
    row = db.get_account(account_id)
    if not row:
        _fail(ValueError(f"unknown account_id {account_id}"), 404)
    s = account_store.get_settings(account_id)
    return {
        "id": account_id, "label": row["label"],
        "pornolab_username": s.get("pornolab_username"),
        "pornolab_password": s.get("pornolab_password"),
        "email_address": row["email_address"],
        "email_password": row["email_password"],
        "email_provider": row["email_provider"],
    }


@app.get("/api/accounts/{account_id}/settings")
async def get_account_settings(account_id: int):
    if not db.get_account(account_id):
        _fail(ValueError(f"unknown account_id {account_id}"), 404)
    return account_store.public(account_id)


@app.post("/api/accounts/{account_id}/settings")
async def set_account_settings(account_id: int, request: Request):
    eng = _engine(account_id)
    before = (eng.s.qbit_category, eng.s.qbit_save_path, eng.s.pornolab_url)
    try:
        account_store.update_settings(account_id, await _body(request))
    except (TypeError, ValueError) as e:
        _fail(e)
    after = (eng.s.qbit_category, eng.s.qbit_save_path, eng.s.pornolab_url)
    if before != after:
        eng.reconfigure()
    eng.db.event("Account settings saved")
    return account_store.public(account_id)


# -------------------------------------------------------------------------- #
# per-account dashboard/state
# -------------------------------------------------------------------------- #
@app.get("/api/state")
async def state(account_id: int = 1):
    return _engine(account_id).state()


@app.get("/api/settings")
async def get_settings():
    return global_store.public()


@app.post("/api/settings")
async def set_settings(request: Request):
    before = (global_store.settings.qbit_url, global_store.settings.qbit_username,
             global_store.settings.qbit_password, global_store.settings.pornolab_url)
    try:
        global_store.update(await _body(request))
    except (TypeError, ValueError) as e:
        _fail(e)
    after = (global_store.settings.qbit_url, global_store.settings.qbit_username,
            global_store.settings.qbit_password, global_store.settings.pornolab_url)
    if before != after:
        for eng in engines.values():
            eng.reconfigure()
    if engines:
        next(iter(engines.values())).db.event("Global settings saved")
    return global_store.public()


@app.get("/api/tailscale/status")
async def tailscale_status():
    """Current egress state -- used both by the Settings tab and by the
    small badge every page shows in the top-right corner. Reports
    configured:false whenever the tailscale sidecar/socket isn't present,
    which the UI renders as plain 'Direct' with no tailscale controls."""
    return await _ts.status()


@app.post("/api/tailscale/login")
async def tailscale_login():
    try:
        return await _ts.login()
    except _ts.TailscaleError as e:
        _fail(e, 502)


@app.post("/api/tailscale/logout")
async def tailscale_logout():
    await _ts.logout()
    return {"ok": True}


@app.post("/api/tailscale/exit-node")
async def tailscale_exit_node(request: Request):
    b = await _body(request)
    node_id = (b.get("node_id") or "").strip() or None
    try:
        await _ts.set_exit_node(node_id)
    except _ts.TailscaleError as e:
        _fail(e, 502)
    return await _ts.status()


@app.post("/api/auto")
async def set_auto(request: Request):
    b = await _body(request)
    account_id = int(b.get("account_id") or 1)
    eng = _engine(account_id)
    on = bool(b.get("enabled"))
    eng.store.update({"auto": on})
    eng.db.event(f"Automatic seeding {'on' if on else 'off'}")
    if on:
        await eng.decide()
    return {"auto": on}


@app.post("/api/guard-override")
async def set_guard_override(request: Request):
    b = await _body(request)
    account_id = int(b.get("account_id") or 1)
    eng = _engine(account_id)
    on = bool(b.get("enabled"))
    eng.store.update({"guard_override": on})
    eng.db.event(f"Download guard override {'enabled' if on else 'disabled'}", "warning" if on else "info")
    await eng.decide()      # re-rank and, if auto is on, act on it now rather than up to DECIDE_INTERVAL later
    return {"guard_override": on}


@app.post("/api/pornolab/login")
async def pl_login(request: Request):
    b = await _body(request)
    account_id = int(b.get("account_id") or 1)
    eng = _engine(account_id)
    username = (b.get("username") or eng.s.pornolab_username or "").strip()
    password = b.get("password") or eng.s.pornolab_password
    if not username or not password:
        _fail(ValueError("username and password are required"))
    return await eng.login(username, password, bool(b.get("remember", True)), b.get("captcha") or None)


@app.post("/api/pornolab/logout")
async def pl_logout(request: Request):
    b = await _body(request)
    eng = _engine(int(b.get("account_id") or 1))
    eng.logout()
    return {"ok": True}


@app.post("/api/profile/refresh")
async def profile_refresh(request: Request):
    b = await _body(request)
    eng = _engine(int(b.get("account_id") or 1))
    try:
        await eng.refresh_profile()
    except PornolabError as e:
        _fail(e, 502)
    return {"ok": True}


@app.post("/api/scan")
async def scan(request: Request):
    b = await _body(request)
    eng = _engine(int(b.get("account_id") or 1))
    try:
        await eng.scan()
    except PornolabError as e:
        _fail(e, 502)
    return eng.state()["scan"]


@app.post("/api/candidates/{topic_id}/add")
async def add_candidate(topic_id: int, request: Request):
    b = await _body(request)
    eng = _engine(int(b.get("account_id") or 1))
    try:
        meta = await eng.add(topic_id)
    except (ValueError, PornolabError, QbitError) as e:
        _fail(e)
    return {"ok": True, **meta}


@app.post("/api/torrents/{infohash}/{action}")
async def torrent_action(infohash: str, action: str, request: Request):
    b = await _body(request)
    eng = _engine(int(b.get("account_id") or 1))
    try:
        if action == "stop":
            await eng.qb.stop([infohash])
        elif action == "start":
            await eng.qb.start([infohash])
        elif action == "remove":
            await eng.remove(infohash, bool(b.get("delete_files", True)))
        else:
            _fail(ValueError(f"unknown action {action}"), 404)
    except QbitError as e:
        _fail(e, 502)
    if action != "remove":      # remove() logs its own event, with the reason
        eng.db.event(f"{action.capitalize()} {infohash[:8]}")
    return {"ok": True}


def _fake_upload_delta_bytes(token: str, mb_per_hour: int) -> int:
    """Return a randomised per-announce upload delta that averages to
    mb_per_hour over a day, with wide, non-repeating variance.

    Design goals:
    - Daily total stays within ±10 % of target (mb_per_hour × 24 hours).
    - No fixed period: combines multiple incommensurate sine waves so the
      pattern never visibly repeats on a human timescale.
    - Extra salt from the announce token keeps torrents out of phase with
      each other, avoiding a suspicious all-torrents-spike-at-once pattern.

    The target per-announce amount is derived from mb_per_hour assuming
    qBittorrent's default 30-minute announce interval.  At other intervals
    the daily total still averages correctly because we key off wall-clock
    time, not announce count.
    """
    target_bytes_per_hour = mb_per_hour * 1024 * 1024
    tok_salt = int.from_bytes(token.encode()[:4].ljust(4, b"\x00"), "big")

    now = time.time()
    # Three incommensurate periods (in seconds): ~47 min, ~1.7 h, ~4.1 h
    phases = [
        math.sin(2 * math.pi * now / 2820 + tok_salt * 0.37),
        math.sin(2 * math.pi * now / 6300 + tok_salt * 0.71),
        math.sin(2 * math.pi * now / 14700 + tok_salt * 1.13),
    ]
    wave = (phases[0] * 0.5 + phases[1] * 0.3 + phases[2] * 0.2)

    multiplier = 1.0 + wave * 0.95
    multiplier = max(0.1, min(3.0, multiplier))
    noise = random.uniform(0.70, 1.30)
    multiplier *= noise

    base = target_bytes_per_hour / 2.0   # bytes per 30-min window
    delta = int(base * multiplier)
    return max(0, delta)


# New-account download bootstrap (see AccountSettings.fake_upload_credit's
# docstring and the plan at ~/.claude/plans/delightful-tickling-parrot.md):
# an account's very first torrent gets its downloaded total faked up toward
# ~1 GiB over its first hour, instead of the usual downloaded=0, so a brand
# new account's PornoLab profile shows a believable small download rather
# than "uploads without ever downloading anything" or "zero downloaded,
# forever". After that hour the total freezes; every other torrent keeps the
# ordinary downloaded=0 behaviour.
BOOTSTRAP_WINDOW_S = 3600
BOOTSTRAP_TARGET_BYTES = 1_000_000_000


def _bootstrap_downloaded_bytes(account_id: int, token: str, first_download_at: float) -> int:
    """Interpolate toward a one-time ±10% target, frozen after the window."""
    key = f"bootstrap_target:{token}"
    cached = db.meta(account_id, key)
    if cached is None:
        target = int(BOOTSTRAP_TARGET_BYTES * random.uniform(0.9, 1.1))
        db.set_meta(account_id, key, target)
    else:
        target = int(cached)

    elapsed = time.time() - first_download_at
    if elapsed >= BOOTSTRAP_WINDOW_S:
        return target
    frac = max(0.0, min(1.0, elapsed / BOOTSTRAP_WINDOW_S))
    # Ease toward the target with a little jitter so it isn't a perfectly
    # straight ramp (announce-to-announce noise, same spirit as the fake
    # upload curve above), but always monotonically non-decreasing in
    # expectation and capped at the target.
    jitter = random.uniform(0.95, 1.05)
    return max(0, min(target, int(target * frac * jitter)))


@app.get("/proxy/ann")
async def announce_proxy(request: Request):
    """Relay a tracker announce, reporting downloaded=0 (or, for a new
    account's bootstrap torrent, a ramping fake total — see
    _bootstrap_downloaded_bytes).

    Forwards the raw query string: info_hash/peer_id are binary and must not be
    decoded and re-encoded. The tracker would otherwise see the peer at this
    app's address, so the VPN address qBittorrent reports is passed as ip=.

    When fake_upload_credit is enabled for the owning account, each announce
    also inflates the reported uploaded counter by a randomised delta keyed to
    the announce token so every torrent stays out of phase with the others —
    but only once that account has a real first-download bootstrap recorded,
    so a brand new account never shows uploads with no download history.
    """
    import re
    token = request.query_params.get("t", "")
    account_id = db.account_for_token(token)
    real = db.meta(account_id, f"ann:{token}") if account_id else None
    if not real or not account_id:
        return Response(b"d14:failure reason9:bad tokene", media_type="text/plain")
    raw = re.sub(r"(^|&)t=[^&]*", "", request.url.query).lstrip("&")

    account_row = db.get_account(account_id)
    first_download_at = account_row["first_download_at"] if account_row else None
    bootstrap_token = account_row["first_download_token"] if account_row else None
    if (bootstrap_token == token and first_download_at
            and time.time() - first_download_at < BOOTSTRAP_WINDOW_S):
        fake_downloaded = _bootstrap_downloaded_bytes(account_id, token, first_download_at)
        raw = re.sub(r"(^|&)downloaded=\d+", rf"\g<1>downloaded={fake_downloaded}", raw)
    else:
        raw = re.sub(r"(^|&)downloaded=\d+", r"\1downloaded=0", raw)

    s = account_store.get_settings(account_id)
    fake_upload_credit = s.get("fake_upload_credit", False)
    fake_upload_mb_per_hour = s.get("fake_upload_mb_per_hour", 0)
    if fake_upload_credit and fake_upload_mb_per_hour > 0 and first_download_at:
        delta = _fake_upload_delta_bytes(token, fake_upload_mb_per_hour)
        new_raw = re.sub(
            r"(^|&)(uploaded)=(\d+)",
            lambda m: (m.group(1) or "") + "uploaded=" + str(int(m.group(3)) + delta),
            raw,
        )
        if new_raw == raw:
            raw += f"&uploaded={delta}"
        else:
            raw = new_raw

    eng = engines.get(account_id)
    ip = ((eng.qbit.get("transfer") or {}) if eng else {}).get("last_external_address_v4")
    if ip and "&ip=" not in "&" + raw:
        raw += f"&ip={ip}"
    url = real + ("&" if "?" in real else "?") + raw
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
        return Response(content=r.content, media_type="text/plain")
    except Exception as e:
        log.warning("announce proxy error: %s", type(e).__name__)
        return Response(b"d14:failure reason11:proxy errore", media_type="text/plain")


@app.post("/api/proxy-trackers/{infohash}")
async def proxy_trackers(infohash: str, request: Request):
    b = await _body(request)
    eng = _engine(int(b.get("account_id") or 1))
    try:
        return {"ok": True, "rewritten": await eng.proxy_existing(infohash)}
    except QbitError as e:
        _fail(e, 502)


@app.post("/api/accounts/import")
async def import_account(request: Request):
    """Add an already-registered PornoLab account, handed over by
    plab-register once it finishes creating the email, registering, and
    (where supported) activating it. This is the one place a new row lands
    in the `accounts` table outside of `migrate_legacy_single_account` --
    plab-register owns the whole registration/activation flow and this app
    only needs to start seeding with the finished credentials."""
    b = await _body(request)
    pornolab_username = (b.get("pornolab_username") or "").strip()
    pornolab_password = b.get("pornolab_password") or ""
    if not pornolab_username or not pornolab_password:
        _fail(ValueError("pornolab_username and pornolab_password are required"))
    label = (b.get("label") or pornolab_username).strip()

    account_settings = asdict(AccountSettings())
    account_settings["pornolab_username"] = pornolab_username
    account_settings["pornolab_password"] = pornolab_password
    if b.get("qbit_category"):
        account_settings["qbit_category"] = b["qbit_category"]
    if b.get("qbit_save_path"):
        account_settings["qbit_save_path"] = b["qbit_save_path"]

    account_id = db.create_account(
        label=label, settings=account_settings,
        email_address=b.get("email_address") or "",
        email_password=b.get("email_password") or "",
        email_provider=b.get("email_provider") or "",
    )
    eng = _build_engine(account_id)
    engines[account_id] = eng
    eng.start()
    eng.db.event(f"Account imported from plab-register: {pornolab_username}")
    return {"ok": True, "account_id": account_id}


@app.get("/api/events")
async def events(account_id: int = 1, limit: int = 200):
    return db.events(account_id, min(limit, 1000))


@app.get("/api/history")
async def history(account_id: int = 1, days: int = 30):
    return db.history(account_id, time.time() - days * 86400)


@app.get("/api/torrents/history")
async def torrent_history(account_id: int = 1):
    return db.torrents(account_id)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
