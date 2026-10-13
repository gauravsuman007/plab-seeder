"""Lean, qBittorrent-free mode: fake-upload-quota reporting with a web UI.

Runs as a FastAPI app (same image, different entrypoint) alongside a
background loop. No qBittorrent, no download guard, no candidates tab.

What it does:
- Web UI with login and a dashboard showing profile, announce status, and
  fake upload/download settings.
- Checks the account profile on first login: only if it shows zero downloads
  ever does it fetch one real small .torrent from the tracker listing (counts
  against the daily quota), recording the real announce URL.
- Then periodically announces straight to the tracker's announce URL with a
  fabricated peer and the same fake downloaded/uploaded math the full app's
  announce proxy uses (app/fake_upload.py), with no actual bittorrent traffic.

State is persisted in a small SQLite DB (shared schema with the full app via
store.py) so restarts continue correctly without re-fetching or losing totals.

For a full seeder (qBittorrent + download guard + candidates), use the main
app instead: app/main.py with docker-compose.yml or docker-compose.standalone.yml.
"""

import asyncio
import base64
import logging
import os
import random
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import fake_upload
from .bencode import BencodeError, decode, torrent_meta
from .parsing import Release
from .pornolab import CaptchaRequired, NotLoggedIn, Pornolab, PornolabError
from .store import (
    DB, GlobalSettingsStore, AccountStore, AccountSettingsHandle, AccountDB,
    AccountSettings, migrate_legacy_single_account,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fake-quota")

HERE = Path(__file__).parent
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# Candidate selection for the one bootstrap download
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "3"))
SCAN_ORDER = int(os.environ.get("SCAN_ORDER", "11"))   # tracker.php o= (11: leechers)
MIN_LEECHERS = int(os.environ.get("MIN_LEECHERS", "1"))
MIN_SIZE_MB = int(os.environ.get("MIN_SIZE_MB", "20"))
MAX_SIZE_MB = int(os.environ.get("MAX_SIZE_MB", "1024"))
ANNOUNCE_INTERVAL_S = int(os.environ.get("ANNOUNCE_INTERVAL_S", str(30 * 60)))
BOOTSTRAP_CHECK_INTERVAL_S = int(os.environ.get("BOOTSTRAP_CHECK_INTERVAL_S", "300"))

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
# qBittorrent 4.6.0 user-agent and peer-id prefix used for tracker announces.
# The tracker sees a normal client; Firefox UA is still used for site HTTP.
ANNOUNCE_UA = "qBittorrent/4.6.0"
PEER_ID_PREFIX = b"-qB4600-"    # 8 bytes; 12 random URL-safe bytes follow

global_store = GlobalSettingsStore()
db = DB()
account_store = AccountStore(db)
_account_id = migrate_legacy_single_account(db, global_store)

# Per-account cookie file, same convention as main.py
from .store import DATA_DIR as _DATA_DIR
_legacy_cookies = _DATA_DIR / "pornolab_cookies.json"
_new_cookies = _DATA_DIR / f"pornolab_cookies_{_account_id}.json"
if _legacy_cookies.exists() and not _new_cookies.exists():
    _new_cookies.write_bytes(_legacy_cookies.read_bytes())
    _new_cookies.chmod(0o600)

_cookies_path = _DATA_DIR / f"pornolab_cookies_{_account_id}.json"
_store_handle = AccountSettingsHandle(global_store, account_store, _account_id)
_db = AccountDB(db, _account_id)

# Pornolab client is created lazily in lifespan so asyncio.Lock() is
# constructed inside the running event loop, not at module import time.
pl: Pornolab | None = None
logged_in = False
profile = None
profile_at: float | None = None
loop_status: dict = {"phase": "idle", "last_announce_at": None, "bootstrap_done": False,
                     "bootstrap_skipped": False, "bootstrap_skip_reason": None,
                     "uploaded_total": 0, "downloaded_total": 0}


# ---- background loop helpers ------------------------------------------------

def _pick_candidate(releases: list[Release]) -> Release | None:
    candidates = [
        r for r in releases
        if r.downloadable and r.seeders >= 1 and r.leechers >= MIN_LEECHERS
        and MIN_SIZE_MB * 1024**2 <= r.size <= MAX_SIZE_MB * 1024**2
    ]
    return min(candidates, key=lambda r: r.size) if candidates else None


def _extract_announce(data: bytes) -> str:
    torrent = decode(data)
    if not isinstance(torrent, dict) or b"announce" not in torrent:
        raise BencodeError("no announce URL in .torrent")
    return torrent[b"announce"].decode("utf-8", "replace")


async def _bootstrap() -> None:
    """Fetch one .torrent for a brand-new account, record the announce URL."""
    global loop_status
    if not logged_in or pl is None:
        return

    try:
        p = await pl.profile()
    except NotLoggedIn:
        return

    if p.live_downloaded:
        log.info("account already has %s bytes downloaded -- skipping bootstrap", p.live_downloaded)
        loop_status.update(bootstrap_skipped=True,
                           bootstrap_skip_reason="account already has download history")
        _db.event("Bootstrap skipped: account already has download history")
        return

    releases: list[Release] = []
    for page in range(SCAN_PAGES):
        try:
            releases += await pl.tracker_page(page, SCAN_ORDER)
        except NotLoggedIn:
            return

    candidate = _pick_candidate(releases)
    if not candidate:
        log.info("no eligible candidate found -- will retry")
        return

    try:
        data = await pl.download(candidate.topic_id)
    except PornolabError as e:
        log.warning("bootstrap download failed: %s", e)
        _db.event(f"Bootstrap download failed: {e}", "error")
        return

    try:
        meta = torrent_meta(data)
        announce_url = _extract_announce(data)
    except BencodeError as e:
        log.warning("bootstrap .torrent unreadable: %s", e)
        _db.event(f"Bootstrap .torrent unreadable: {e}", "error")
        return

    # peer_id: qBittorrent format (-qB4600- + 12 random URL-safe ASCII chars).
    # Stored as hex so it round-trips cleanly through SQLite.
    peer_id_bytes = PEER_ID_PREFIX + secrets.token_bytes(12)
    peer_id_hex = peer_id_bytes.hex()
    # key: a random 32-bit value qBittorrent includes on every announce so
    # the tracker can correlate re-announces from the same client instance.
    announce_key = secrets.token_hex(4)
    port = random.randint(6881, 6999)
    bootstrap_target = fake_upload.bootstrap_target_bytes()
    now = time.time()

    _db.set_meta("fq:topic_id", candidate.topic_id)
    _db.set_meta("fq:infohash", meta["infohash"])
    _db.set_meta("fq:announce_url", announce_url)
    _db.set_meta("fq:peer_id", peer_id_hex)
    _db.set_meta("fq:announce_key", announce_key)
    _db.set_meta("fq:port", port)
    _db.set_meta("fq:first_download_at", now)
    _db.set_meta("fq:bootstrap_target", bootstrap_target)
    _db.set_meta("fq:uploaded_total", 0)
    _db.set_meta("fq:announced_at", "")

    _db.set_first_download(peer_id_hex)  # mark first_download_at on the accounts row
    loop_status.update(bootstrap_done=True)
    log.info("bootstrap: fetched '%s' (%s bytes) as topic %s",
             candidate.title, meta["size"], candidate.topic_id)
    _db.event(f"Bootstrap: fetched '{candidate.title}' ({meta['size']} bytes) as topic {candidate.topic_id}")


async def _announce_once() -> None:
    """Send one direct announce to the real tracker URL."""
    global loop_status

    infohash = _db.meta("fq:infohash")
    announce_url = _db.meta("fq:announce_url")
    peer_id_hex = _db.meta("fq:peer_id")
    announce_key = _db.meta("fq:announce_key")
    first_download_at = _db.meta("fq:first_download_at")
    bootstrap_target = _db.meta("fq:bootstrap_target")
    port = _db.meta("fq:port")
    uploaded_total = int(_db.meta("fq:uploaded_total") or 0)
    downloaded_extra = int(_db.meta("fq:downloaded_extra") or 0)

    if not all([infohash, announce_url, peer_id_hex, first_download_at, bootstrap_target]):
        return

    # Migrate old 10-byte random peer_id to qBittorrent format (20 bytes).
    if len(peer_id_hex) != 40:
        peer_id_bytes_new = PEER_ID_PREFIX + secrets.token_bytes(12)
        peer_id_hex = peer_id_bytes_new.hex()
        _db.set_meta("fq:peer_id", peer_id_hex)
        _db.set_meta("fq:announced_at", "")   # force event=started with the new id
    if not announce_key:
        announce_key = secrets.token_hex(4)
        _db.set_meta("fq:announce_key", announce_key)

    first_at = float(first_download_at)
    target = int(bootstrap_target)
    s = _store_handle.settings

    bootstrap_dl = fake_upload.bootstrap_downloaded_bytes(target, first_at)
    if s.fake_upload_credit and s.fake_upload_mb_per_hour > 0:
        uploaded_total += fake_upload.fake_upload_delta_bytes(infohash, s.fake_upload_mb_per_hour)
    if s.fake_download_credit and s.fake_download_mb_per_hour > 0:
        downloaded_extra += fake_upload.fake_download_delta_bytes(infohash, s.fake_download_mb_per_hour)
    _db.set_meta("fq:uploaded_total", uploaded_total)
    _db.set_meta("fq:downloaded_extra", downloaded_extra)
    downloaded = bootstrap_dl + downloaded_extra

    info_hash_bytes = bytes.fromhex(infohash)
    peer_id_bytes = bytes.fromhex(peer_id_hex)
    is_first = not _db.meta("fq:announced_at")
    qs = (
        f"info_hash={quote(info_hash_bytes, safe='')}"
        f"&peer_id={quote(peer_id_bytes, safe='')}"
        f"&port={port}&uploaded={uploaded_total}&downloaded={downloaded}"
        f"&left=0&compact=1&numwant=200"
    )
    if announce_key:
        qs += f"&key={announce_key}"
    if is_first:
        qs += "&event=started"
    sep = "&" if "?" in announce_url else "?"
    url = announce_url + sep + qs

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": ANNOUNCE_UA, "Accept-Encoding": "gzip"},
            timeout=30,
        ) as client:
            r = await client.get(url)
        log.info("announced: downloaded=%s (bootstrap=%s extra=%s) uploaded=%s -> HTTP %s",
                 downloaded, bootstrap_dl, downloaded_extra, uploaded_total, r.status_code)
        _db.set_meta("fq:announced_at", time.time())
        loop_status.update(last_announce_at=time.time(), uploaded_total=uploaded_total,
                           downloaded_total=downloaded, downloaded_extra=downloaded_extra)
        if r.status_code != 200:
            _db.event(f"Announce HTTP {r.status_code}", "warning")
    except httpx.HTTPError as e:
        log.warning("announce failed: %s", e)
        _db.event(f"Announce failed: {e}", "warning")


async def _background_loop() -> None:
    global loop_status
    while True:
        try:
            loop_status["phase"] = "checking"

            skipped = _db.meta("fq:skip") or loop_status.get("bootstrap_skipped")
            has_bootstrap = bool(_db.meta("fq:infohash"))

            if not has_bootstrap and not skipped:
                loop_status["phase"] = "bootstrapping"
                await _bootstrap()
                has_bootstrap = bool(_db.meta("fq:infohash"))
                skipped = loop_status.get("bootstrap_skipped")

            if has_bootstrap:
                loop_status["phase"] = "announcing"
                loop_status["bootstrap_done"] = True
                # Restore persisted totals in case of restart
                loop_status["uploaded_total"] = int(_db.meta("fq:uploaded_total") or 0)
                loop_status["downloaded_extra"] = int(_db.meta("fq:downloaded_extra") or 0)
                await _announce_once()

        except Exception as e:  # noqa: BLE001
            log.exception("background loop error: %s", e)
            _db.event(f"Background loop error: {e}", "error")

        loop_status["phase"] = "waiting"
        sleep_s = BOOTSTRAP_CHECK_INTERVAL_S if not has_bootstrap else ANNOUNCE_INTERVAL_S
        await asyncio.sleep(sleep_s)


# ---- FastAPI app -------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pl
    pl = Pornolab(_store_handle.settings.pornolab_url, _cookies_path)
    # Restore logged_in state from saved session cookie if present.
    if _cookies_path.exists():
        global logged_in, profile, profile_at
        try:
            p = await pl.profile()
            logged_in = True
            profile = p
            profile_at = time.time()
            _db.event("Session restored from saved cookies")
        except Exception:  # noqa: BLE001
            pass
    asyncio.create_task(_background_loop())
    yield
    await pl.close()


app = FastAPI(title="pornolab-fake-quota", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
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
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="fake-quota"'})
    return await call_next(request)


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except ValueError:
        return {}


@app.get("/")
async def index():
    return FileResponse(HERE / "templates" / "fake_quota.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/state")
async def state():
    global profile, profile_at, logged_in, loop_status
    s = _store_handle.settings
    account_row = db.get_account(_account_id)
    p = profile

    return {
        "now": time.time(),
        "logged_in": logged_in,
        "pending_captcha": pl.pending_captcha is not None if pl else False,
        "loop": loop_status,
        "fake_upload_credit": s.fake_upload_credit,
        "fake_upload_mb_per_hour": s.fake_upload_mb_per_hour,
        "fake_download_credit": s.fake_download_credit,
        "fake_download_mb_per_hour": s.fake_download_mb_per_hour,
        "bootstrap": {
            "done": bool(_db.meta("fq:infohash")),
            "skipped": bool(_db.meta("fq:skip") or loop_status.get("bootstrap_skipped")),
            "skip_reason": loop_status.get("bootstrap_skip_reason"),
            "infohash": _db.meta("fq:infohash"),
            "topic_id": _db.meta("fq:topic_id"),
            "first_download_at": account_row["first_download_at"] if account_row else None,
            "uploaded_total": int(_db.meta("fq:uploaded_total") or 0),
            "downloaded_total": loop_status.get("downloaded_total", 0),
            "downloaded_extra": int(_db.meta("fq:downloaded_extra") or 0),
            "announced_at": _db.meta("fq:announced_at") or None,
        },
        "profile": None if not p else {
            "at": profile_at,
            "username": p.username, "rating": p.rating, "newbie": p.newbie,
            "downloaded": p.live_downloaded, "credited_upload": p.live_credited_upload,
            "today": {"down": p.down.today, "up": p.up.today, "own": p.own.today, "bonus": p.bonus.today},
        },
    }


@app.post("/api/login")
async def login(request: Request):
    global logged_in, profile, profile_at
    if pl is None:
        return JSONResponse({"ok": False, "error": "app not ready yet"})
    b = await _body(request)
    username = b.get("username", "")
    password = b.get("password", "")
    remember = b.get("remember", True)
    captcha = b.get("captcha") or None

    try:
        await pl.login(username, password, captcha)
    except CaptchaRequired as e:
        return JSONResponse({"ok": False, "captcha": e.image})
    except PornolabError as e:
        return JSONResponse({"ok": False, "error": str(e)})

    _store_handle.update({"pornolab_username": username,
                           "pornolab_password": password if remember else ""})
    logged_in = True
    _db.event(f"Logged in to PornoLab as {username}")

    try:
        profile = await pl.profile()
        profile_at = time.time()
    except PornolabError:
        pass

    return {"ok": True}


@app.post("/api/logout")
async def logout():
    global logged_in, profile, profile_at
    if pl:
        pl.clear_session()
    logged_in = False
    profile = None
    profile_at = None
    _store_handle.update({"pornolab_password": ""})
    _db.event("Logged out of PornoLab")
    return {"ok": True}


@app.post("/api/refresh_profile")
async def refresh_profile():
    global logged_in, profile, profile_at
    if pl is None:
        return JSONResponse({"ok": False, "error": "app not ready yet"})
    try:
        profile = await pl.profile()
        profile_at = time.time()
    except NotLoggedIn:
        logged_in = False
        return JSONResponse({"ok": False, "error": "not logged in"})
    except PornolabError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


@app.post("/api/settings")
async def save_settings(request: Request):
    b = await _body(request)
    try:
        account_store.update_settings(_account_id, b)
    except (TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    _db.event("Settings saved")
    s = _store_handle.settings
    return {"ok": True, "fake_upload_credit": s.fake_upload_credit,
            "fake_upload_mb_per_hour": s.fake_upload_mb_per_hour,
            "fake_download_credit": s.fake_download_credit,
            "fake_download_mb_per_hour": s.fake_download_mb_per_hour}


@app.get("/api/events")
async def events():
    return _db.events(100)
