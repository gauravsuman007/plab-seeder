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
import string
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .engine import Engine
from .pornolab import Pornolab, PornolabError, _solve_captcha as _ocr_solve
from .qbit import QbitError
from .store import (
    DB, GlobalSettingsStore, AccountStore, AccountSettingsHandle, AccountDB,
    AccountSettings, migrate_legacy_single_account,
)
from . import parsing as _parsing
from . import temp_email as _te
from . import mail_ru as _mru
from . import gmx as _gmx
from . import tuta as _tuta

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

# Standalone client for the anonymous registration flow -- registering an
# account happens BEFORE that account exists, so it can't use any account's
# Engine/Pornolab instance (and register() already builds its own cookieless
# client internally for the actual submit; this one is only for
# register_form()'s camoufox/patchright Turnstile dance).
_register_pl = Pornolab(global_store.settings.pornolab_url)

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
    await _register_pl.close()


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


_pending_register_form: _parsing.RegisterForm | None = None
_active_temp_address: _te.TempAddress | None = None


@app.get("/api/register/form")
async def register_form():
    """Fetch the PornoLab registration page and return the image captcha + lists."""
    global _pending_register_form
    try:
        form = await _register_pl.register_form()
    except PornolabError as e:
        _fail(e, 502)
    _pending_register_form = form
    rendered = await _render_register_form(form)
    rendered.pop("captcha_bytes", None)   # raw image bytes, internal use by /api/register/auto only
    return rendered


async def _render_register_form(form: _parsing.RegisterForm) -> dict:
    # Fetch the captcha using a client seeded with the browser's anonymous
    # session cookies — the captcha is tied to that session, not the owner's.
    img_data = None
    captcha_text = None
    img_bytes = None
    if form.captcha_url:
        try:
            async with httpx.AsyncClient(timeout=15) as _cap_client:
                for c in form.browser_cookies:
                    _cap_client.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."))
                img = await _cap_client.get(
                    form.captcha_url,
                    headers={"Referer": f"{_register_pl.base}/forum/profile.php?mode=register"},
                )
            mime = img.headers.get("content-type", "image/png").split(";")[0]
            if mime.startswith("image/"):
                img_bytes = img.content
                img_data = f"data:{mime};base64,{base64.b64encode(img.content).decode()}"
                try:
                    captcha_text = _ocr_solve(img.content)
                except Exception as e:
                    log.warning("captcha OCR failed: %s", e)
        except Exception:
            pass
    return {
        "captcha": img_data,
        "captcha_text": captcha_text,
        "captcha_bytes": img_bytes,   # internal use by /api/register/auto, stripped before returning to the UI
        "countries": form.countries,
        "timezones": form.timezones,
        "turnstile_solved": bool(form.turnstile_token),
        "_debug_cap_sid": form.cap_sid,
        "_debug_cap_field": form.cap_field,
        "_debug_captcha_url": form.captcha_url,
        "_debug_browser_cookie_names": [c["name"] for c in form.browser_cookies],
    }


@app.post("/api/register")
async def register(request: Request):
    global _pending_register_form
    b = await _body(request)
    if not _pending_register_form:
        _fail(ValueError("fetch the registration form first (/api/register/form)"))
    try:
        result = await _register_pl.register(
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
    return result


def _is_captcha_error(error: str) -> bool:
    return "код подтверждения" in (error or "")


def _rand_username(n: int = 10) -> str:
    return "pl_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _rand_password(n: int = 16) -> str:
    chars = string.ascii_letters + string.digits
    pw = list(random.choices(chars, k=n))
    pw[0] = random.choice(string.ascii_uppercase)
    pw[1] = random.choice(string.ascii_lowercase)
    pw[2] = random.choice(string.digits)
    pw[3] = random.choice(string.digits)
    random.shuffle(pw)
    return "".join(pw)


@app.post("/api/register/auto")
async def register_auto(request: Request):
    """Create an email, register a PornoLab account with it, and (where the
    email provider supports it) activate it automatically. Captcha/username
    failures are retried with fresh values, up to 10 attempts total.

    On success the new account is added to the `accounts` table immediately
    (even if activation couldn't be confirmed automatically) — the owner can
    always finish activating by hand from the credentials it returns, and per
    the "never throw away a registered account" rule nothing here discards
    anything once registration itself succeeds.
    """
    b = await _body(request)
    provider = (b.get("provider") or global_store.settings.email_factory_provider or "tuta").lower()

    if provider == "tuta":
        sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
        if not sidecar:
            _fail(ValueError("TURNSTILE_SIDECAR_URL not set"))
        factory = _tuta.TutaFactory(sidecar_url=sidecar)
        try:
            email, email_password = await factory.create()
        except _tuta.TutaError as e:
            _fail(e, 502)
    elif provider == "gmx":
        factory = _gmx.GmxFactory(provider=global_store.settings.gmx_provider or "web.de")
        try:
            email, email_password = await factory.create()
        except _gmx.GmxError as e:
            _fail(e, 502)
    elif provider == "mailru":
        key = global_store.settings.sms_activate_api_key
        if not key:
            _fail(ValueError("sms_activate_api_key not set in settings"))
        factory = _mru.MailRuFactory(sms_api_key=key, domain=global_store.settings.mailru_domain or "mail.ru")
        try:
            email, email_password = await factory.create()
        except (_mru.MailRuError, _mru.SmsActivateError) as e:
            _fail(e, 502)
    else:
        _fail(ValueError(f"unknown provider {provider!r}"))
        return  # unreachable, keeps type checkers happy

    return await _register_pornolab_with_email(email, email_password, provider)


async def _register_pornolab_with_email(email: str, email_password: str, provider: str) -> dict:
    """Register a new PornoLab account against an already-created inbox,
    retrying on captcha/username failures, then add it to the `accounts`
    table and (for providers with automated inbox reading) activate it."""
    username = _rand_username()
    password = _rand_password()
    last_error = None
    result = None
    for attempt in range(10):
        try:
            form = await _register_pl.register_form()
        except PornolabError as e:
            last_error = str(e)
            continue
        rendered = await _render_register_form(form)
        captcha_text = rendered.get("captcha_text") or ""
        try:
            result = await _register_pl.register(
                username=username, password=password, email=email,
                captcha_code=captcha_text, form=form, country="0", timezone="100",
            )
        except PornolabError as e:
            last_error = str(e)
            continue
        if result.get("ok"):
            break
        last_error = result.get("error")
        if not _is_captcha_error(last_error):
            username = _rand_username()   # likely "username taken" or similar — try a new one too
    else:
        _fail(ValueError(f"registration failed after 10 attempts: {last_error}"), 502)

    if not (result and result.get("ok")):
        _fail(ValueError(f"registration failed: {last_error}"), 502)

    account_settings = asdict(AccountSettings())
    account_settings["pornolab_username"] = username
    account_settings["pornolab_password"] = password
    account_id = db.create_account(
        label=username, settings=account_settings,
        email_address=email, email_password=email_password, email_provider=provider,
    )
    eng = _build_engine(account_id)
    engines[account_id] = eng
    eng.start()

    activation_url = None
    if provider == "tuta":
        sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
        try:
            activation_url = await _tuta.TutaFactory(sidecar_url=sidecar).find_activation(
                email, email_password, timeout=150,
            )
            if activation_url:
                async with httpx.AsyncClient(timeout=20) as c:
                    await c.get(activation_url)
        except _tuta.TutaError as e:
            log.warning("auto-activation failed for account %s: %s", account_id, e)

    eng.db.event(f"Account auto-registered: {username} / {email}"
                 + (" (activated)" if activation_url else " (activation pending — check inbox)"))
    return {
        "ok": True, "account_id": account_id, "username": username, "password": password,
        "email": email, "email_password": email_password, "provider": provider,
        "activated": bool(activation_url),
    }


@app.get("/api/tempemail/providers")
async def tempemail_providers():
    return {"providers": _te.provider_names()}


@app.post("/api/tempemail/new")
async def tempemail_new(request: Request):
    global _active_temp_address
    b = await _body(request)
    provider = b.get("provider") or None
    try:
        addr = await _te.new_address(provider)
    except _te.ProviderError as e:
        _fail(e, 502)
    _active_temp_address = addr
    return {"address": addr.address, "provider": addr.provider}


@app.get("/api/tempemail/inbox")
async def tempemail_inbox():
    if not _active_temp_address:
        _fail(ValueError("no active temp address – call /api/tempemail/new first"))
    try:
        msgs = await _active_temp_address.inbox()
    except _te.ProviderError as e:
        _fail(e, 502)
    return {
        "address": _active_temp_address.address,
        "provider": _active_temp_address.provider,
        "messages": [
            {
                "id": m.id,
                "from": m.from_addr,
                "subject": m.subject,
                "body_text": m.body_text,
                "body_html": m.body_html,
            }
            for m in msgs
        ],
    }


@app.get("/api/tempemail/message/{msg_id}")
async def tempemail_message(msg_id: str):
    if not _active_temp_address:
        _fail(ValueError("no active temp address"))
    try:
        m = await _active_temp_address.message(msg_id)
    except _te.ProviderError as e:
        _fail(e, 502)
    return {
        "id": m.id,
        "from": m.from_addr,
        "subject": m.subject,
        "body_text": m.body_text,
        "body_html": m.body_html,
    }


@app.post("/api/email/create")
async def email_create(request: Request):
    """Create a fresh email account automatically (no PornoLab account).

    provider:
      "tuta"   → @tutamail.com, no phone, no captcha. The only provider that's
                 actually phone-free end to end — recommended default.
                 Requires TURNSTILE_SIDECAR_URL (reuses the camoufox sidecar).
      "gmx"    → @web.de / @gmx.net. DEAD END: requires a phone number at the
                 final registration step — see app/gmx.py's module docstring.
      "mailru" → @list.ru / @mail.ru via VK ID + sms-activate.org.
                 Requires sms_activate_api_key in settings.
    """
    b = await _body(request)
    provider = (b.get("provider") or global_store.settings.email_factory_provider or "tuta").lower()

    if provider == "tuta":
        sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
        if not sidecar:
            _fail(ValueError("TURNSTILE_SIDECAR_URL not set — the tuta provider reuses that sidecar's camoufox browser"))
        factory = _tuta.TutaFactory(sidecar_url=sidecar)
        try:
            email, password = await factory.create()
        except _tuta.TutaError as e:
            _fail(e, 502)
        if engines:
            next(iter(engines.values())).db.event(f"email account created: {email}")
        return {"ok": True, "email": email, "password": password}

    if provider == "gmx":
        gmx_provider = global_store.settings.gmx_provider or "web.de"
        factory = _gmx.GmxFactory(provider=gmx_provider)
        try:
            email, password = await factory.create()
        except _gmx.GmxError as e:
            _fail(e, 502)
        return {"ok": True, "email": email, "password": password}

    if provider == "mailru":
        key = global_store.settings.sms_activate_api_key
        if not key:
            _fail(ValueError("sms_activate_api_key not set in settings"))
        domain = global_store.settings.mailru_domain or "mail.ru"
        factory = _mru.MailRuFactory(sms_api_key=key, domain=domain)
        try:
            email, password = await factory.create()
        except _mru.MailRuError as e:
            _fail(e, 502)
        except _mru.SmsActivateError as e:
            _fail(e, 502)
        return {"ok": True, "email": email, "password": password}

    _fail(ValueError(f"unknown provider {provider!r}; choose 'tuta', 'gmx', or 'mailru'"))


@app.post("/api/email/activation")
async def email_activation(request: Request):
    """Poll an email account for the PornoLab activation link.

    Only "tuta" is supported here — gmx/mailru's IMAP-based activation lookup
    is a separate code path (gmx_find_activation / mailru_find_activation)
    that the UI doesn't currently call.
    """
    b = await _body(request)
    email = (b.get("email") or "").strip()
    password = b.get("password") or ""
    if not email or not password:
        _fail(ValueError("email and password are required"))
    sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
    if not sidecar:
        _fail(ValueError("TURNSTILE_SIDECAR_URL not set"))
    factory = _tuta.TutaFactory(sidecar_url=sidecar)
    try:
        url = await factory.find_activation(email, password, timeout=int(b.get("timeout") or 120))
    except _tuta.TutaError as e:
        _fail(e, 502)
    return {"ok": True, "url": url}


async def _list_inbox(provider: str, address: str, password: str) -> list[dict]:
    """Dispatch to the right inbox-listing mechanism for a provider. Raises
    ValueError for providers with no inbox-reading support."""
    if provider == "tuta":
        sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
        if not sidecar:
            raise ValueError("TURNSTILE_SIDECAR_URL not set")
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{sidecar.rstrip('/')}/tuta/list", json={"email": address, "password": password})
            r.raise_for_status()
            d = r.json()
        if not d.get("ok"):
            raise ValueError(d.get("error") or "inbox listing failed")
        return d.get("messages", [])
    if provider == "gmx":
        return await _gmx.gmx_list_inbox(address, password)
    if provider == "mailru":
        return await _mru.mailru_list_inbox(address, password)
    raise ValueError(f"inbox listing is not supported for provider {provider!r}")


@app.get("/api/email/inbox")
async def email_inbox(account_id: int):
    """List recent inbox subjects for an account's email, so the owner can
    see whether the activation email arrived without leaving the app."""
    row = db.get_account(account_id)
    if not row:
        _fail(ValueError(f"unknown account_id {account_id}"), 404)
    try:
        messages = await _list_inbox(row["email_provider"], row["email_address"], row["email_password"])
    except ValueError as e:
        _fail(e, 502)
    return {"ok": True, "messages": messages}


# --------------------------------------------------------------------------- #
# standalone email registry -- create/inspect/discard email accounts across
# all the providers we support, independent of any PornoLab registration.
# --------------------------------------------------------------------------- #

_EMAIL_PROVIDERS = ["tuta", "gmx", "atomicmail"]  # mail.ru needs a phone number via sms-activate -- excluded


@app.get("/api/emails/providers")
async def emails_providers():
    return {"providers": _EMAIL_PROVIDERS}


@app.get("/api/emails")
async def emails_list():
    return db.list_emails()


@app.post("/api/emails/new")
async def emails_new(request: Request):
    """Create a new email account with the given provider and bank it in the
    emails registry. atomicmail.io needs a human to clear its hCaptcha and
    goes through /api/atomicmail/signup/* instead -- this endpoint covers the
    providers whose signup is fully scriptable."""
    b = await _body(request)
    provider = (b.get("provider") or "").lower()
    if provider == "tuta":
        sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
        if not sidecar:
            _fail(ValueError("TURNSTILE_SIDECAR_URL not set"))
        try:
            address, password = await _tuta.TutaFactory(sidecar_url=sidecar).create()
        except _tuta.TutaError as e:
            _fail(e, 502)
    elif provider == "gmx":
        try:
            address, password = await _gmx.GmxFactory(provider=global_store.settings.gmx_provider or "web.de").create()
        except _gmx.GmxError as e:
            _fail(e, 502)
    elif provider == "mailru":
        _fail(ValueError("mail.ru needs a phone number for signup — not offered here"))
        return  # unreachable
    elif provider == "atomicmail":
        _fail(ValueError("atomicmail.io needs a human to clear its hCaptcha — "
                          "use POST /api/atomicmail/signup/start instead"))
        return  # unreachable
    else:
        _fail(ValueError(f"unknown provider {provider!r}"))
        return  # unreachable

    email_id = db.create_email(provider=provider, address=address, password=password)
    return {"ok": True, "id": email_id, "provider": provider, "address": address, "password": password}


@app.get("/api/emails/{email_id}/credentials")
async def email_credentials(email_id: int):
    row = db.get_email(email_id)
    if not row:
        _fail(ValueError(f"unknown email id {email_id}"), 404)
    return {"id": email_id, "address": row["address"], "password": row["password"], "provider": row["provider"]}


@app.get("/api/emails/{email_id}/inbox")
async def emails_inbox(email_id: int):
    row = db.get_email(email_id)
    if not row:
        _fail(ValueError(f"unknown email id {email_id}"), 404)
    try:
        messages = await _list_inbox(row["provider"], row["address"], row["password"])
    except ValueError as e:
        _fail(e, 502)
    return {"ok": True, "messages": messages}


@app.delete("/api/emails/{email_id}")
async def emails_delete(email_id: int):
    """Drop an email from the registry (e.g. the owner found it can no
    longer log in). This only forgets it locally -- it does not reach out to
    the provider to delete the remote account."""
    row = db.get_email(email_id)
    if not row:
        _fail(ValueError(f"unknown email id {email_id}"), 404)
    db.delete_email(email_id)
    return {"ok": True}


@app.post("/api/emails/{email_id}/register-pornolab")
async def emails_register_pornolab(email_id: int):
    """Take an already-created email from the registry and run it through
    the PornoLab registration pipeline, same as /api/register/auto does for
    a freshly-created one. On success the email is removed from the
    standalone registry since it now lives on the new PornoLab account."""
    row = db.get_email(email_id)
    if not row:
        _fail(ValueError(f"unknown email id {email_id}"), 404)
    result = await _register_pornolab_with_email(row["address"], row["password"], row["provider"])
    if result.get("ok"):
        db.delete_email(email_id)
    return result


@app.post("/api/atomicmail/signup/start")
async def atomicmail_signup_start():
    """Start an interactive atomicmail.io signup. atomicmail.io is accepted
    by PornoLab's registration form (confirmed live) and has no phone
    requirement, but its final step is gated by hCaptcha, which doesn't
    auto-clear the way Cloudflare Turnstile does — so this hands the real
    challenge screenshot back to the owner to click through, via
    /api/atomicmail/signup/click and /verify below."""
    sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
    if not sidecar:
        _fail(ValueError("TURNSTILE_SIDECAR_URL not set"))
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{sidecar.rstrip('/')}/atomicmail/signup/start")
            r.raise_for_status()
            d = r.json()
    except httpx.HTTPError as e:
        _fail(e, 502)
    if not d.get("ok"):
        _fail(ValueError(d.get("error") or "atomicmail signup failed to start"), 502)
    return d


@app.post("/api/atomicmail/signup/click")
async def atomicmail_signup_click(request: Request):
    b = await _body(request)
    session_id = b.get("session_id") or ""
    if not session_id:
        _fail(ValueError("session_id is required"))
    sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
    if not sidecar:
        _fail(ValueError("TURNSTILE_SIDECAR_URL not set"))
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{sidecar.rstrip('/')}/atomicmail/signup/click", json={
                "session_id": session_id, "x": b.get("x"), "y": b.get("y"),
            })
            r.raise_for_status()
            d = r.json()
    except httpx.HTTPError as e:
        _fail(e, 502)
    if not d.get("ok"):
        _fail(ValueError(d.get("error") or "click failed"), 502)
    return d


@app.post("/api/atomicmail/signup/verify")
async def atomicmail_signup_verify(request: Request):
    b = await _body(request)
    session_id = b.get("session_id") or ""
    if not session_id:
        _fail(ValueError("session_id is required"))
    sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
    if not sidecar:
        _fail(ValueError("TURNSTILE_SIDECAR_URL not set"))
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{sidecar.rstrip('/')}/atomicmail/signup/verify", json={
                "session_id": session_id,
            })
            r.raise_for_status()
            d = r.json()
    except httpx.HTTPError as e:
        _fail(e, 502)
    if not d.get("ok"):
        _fail(ValueError(d.get("error") or "verify failed"), 502)
    if d.get("solved"):
        # hCaptcha cleared and atomicmail.io finished creating the inbox --
        # just bank the credentials. Registering it on PornoLab is a
        # separate, explicit step from the Emails tab, not automatic here.
        email_id = db.create_email(provider="atomicmail", address=d["email"], password=d["password"])
        return {"ok": True, "done": True, "solved": True, "email_id": email_id,
                "email": d["email"], "password": d["password"]}
    return d


@app.post("/api/atomicmail/signup/cancel")
async def atomicmail_signup_cancel(request: Request):
    b = await _body(request)
    session_id = b.get("session_id") or ""
    sidecar = os.environ.get("TURNSTILE_SIDECAR_URL")
    if session_id and sidecar:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(f"{sidecar.rstrip('/')}/atomicmail/signup/cancel", json={"session_id": session_id})
        except httpx.HTTPError:
            pass
    return {"ok": True}


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
