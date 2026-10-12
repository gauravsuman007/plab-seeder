"""Lean, qBittorrent-free mode: fake-upload-quota reporting with nothing else.

No FastAPI app, no qBittorrent, no download guard, no disk usage at all
beyond a single small JSON state file. This exists for the case where the
owner doesn't want the full seeder (app/main.py + qBittorrent + the
tunnel/gateway stack) running for an account at all -- just a believable
profile: one real .torrent fetched *only if the account has never
downloaded anything*, then an ongoing fake upload/download trickle reported
straight to the tracker's own announce URL, with no actual peer-to-peer
traffic and no bittorrent client anywhere in the picture.

Flow, run forever as a single asyncio loop:
1. Log in to PornoLab (reusing app/pornolab.py).
2. If no bootstrap torrent is recorded yet, check the profile: only if it
   shows literally zero downloaded ever do we pick one small candidate off
   the tracker listing, fetch its .torrent (counts against the daily quota,
   same as the full app), and record its real tracker announce URL +
   infohash. If the account already has downloads, nothing is ever fetched
   -- there's no second chance to look "new" once that's missed.
3. Once a bootstrap torrent is recorded, periodically announce straight to
   the tracker (not through qBittorrent, which doesn't exist here) with a
   fabricated peer_id/port and the same fake downloaded/uploaded math the
   full app's announce proxy uses (app/fake_upload.py) -- downloaded ramps
   to a ~1GiB target over the first hour then freezes, uploaded grows by a
   randomised delta every announce.

This is a one-shot, one-torrent bootstrap by design: it is meant to make a
brand new account look like it did one small download and has been quietly
seeding since, not to run an ongoing seeding operation. For that, run the
full app (app/main.py) with qBittorrent instead -- see docker-compose.yml.
"""

import asyncio
import json
import logging
import os
import random
import secrets
import time
from urllib.parse import quote

import httpx

from . import fake_upload
from .bencode import BencodeError, decode, torrent_meta
from .parsing import Release
from .pornolab import NotLoggedIn, Pornolab, PornolabError
from .store import DATA_DIR

log = logging.getLogger("fake-quota")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

STATE_PATH = DATA_DIR / "fake_quota_state.json"
HEARTBEAT_PATH = DATA_DIR / "fake_quota_heartbeat"

PORNOLAB_URL = os.environ.get("PORNOLAB_URL", "https://pornolab.net")
PORNOLAB_USERNAME = os.environ.get("PORNOLAB_USERNAME", "")
PORNOLAB_PASSWORD = os.environ.get("PORNOLAB_PASSWORD", "")

SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "3"))
SCAN_ORDER = int(os.environ.get("SCAN_ORDER", "11"))          # tracker.php o= (11: leechers)
MIN_LEECHERS = int(os.environ.get("MIN_LEECHERS", "1"))
MIN_SIZE_MB = int(os.environ.get("MIN_SIZE_MB", "20"))
MAX_SIZE_MB = int(os.environ.get("MAX_SIZE_MB", "1024"))

FAKE_UPLOAD_MB_PER_HOUR = int(os.environ.get("FAKE_UPLOAD_MB_PER_HOUR", "250"))
ANNOUNCE_INTERVAL_S = int(os.environ.get("ANNOUNCE_INTERVAL_S", str(30 * 60)))
BOOTSTRAP_CHECK_INTERVAL_S = int(os.environ.get("BOOTSTRAP_CHECK_INTERVAL_S", "300"))

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def _heartbeat() -> None:
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(str(time.time()))
    except OSError:
        pass


def _pick_candidate(releases: list[Release], known: set[int]) -> Release | None:
    """Smallest eligible release not already known -- a quick, unremarkable
    first download rather than anything that stands out."""
    candidates = [
        r for r in releases
        if r.downloadable and r.topic_id not in known
        and r.seeders >= 1 and r.leechers >= MIN_LEECHERS
        and MIN_SIZE_MB * 1024**2 <= r.size <= MAX_SIZE_MB * 1024**2
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r.size)


def _extract_announce(data: bytes) -> str:
    torrent = decode(data)
    if not isinstance(torrent, dict) or b"announce" not in torrent:
        raise BencodeError("no announce URL in .torrent")
    return torrent[b"announce"].decode("utf-8", "replace")


async def _bootstrap(pl: Pornolab, state: dict) -> dict | None:
    """Fetch one real .torrent for a brand-new account. Returns new state
    fields, or None if there's nothing to do (not new, or nothing eligible)."""
    try:
        profile = await pl.profile()
    except NotLoggedIn:
        await pl.login(PORNOLAB_USERNAME, PORNOLAB_PASSWORD)
        profile = await pl.profile()

    if profile.live_downloaded:
        log.info("account already has %s downloaded -- not a new account, nothing to bootstrap",
                 profile.live_downloaded)
        return {"skipped": True, "skipped_reason": "account already has download history"}

    releases: list[Release] = []
    for page in range(SCAN_PAGES):
        releases += await pl.tracker_page(page, SCAN_ORDER)
    candidate = _pick_candidate(releases, known=set())
    if not candidate:
        log.info("no eligible candidate on the scanned pages yet -- will retry")
        return None

    data = await pl.download(candidate.topic_id)
    meta = torrent_meta(data)
    announce_url = _extract_announce(data)

    log.info("bootstrap: fetched '%s' (%s bytes) as topic %s", candidate.title, meta["size"], candidate.topic_id)
    return {
        "topic_id": candidate.topic_id,
        "infohash": meta["infohash"],
        "announce_url": announce_url,
        "peer_id": secrets.token_hex(10),        # 20 bytes
        "port": random.randint(6881, 6999),
        "first_download_at": time.time(),
        "bootstrap_target": fake_upload.bootstrap_target_bytes(),
        "uploaded_total": 0,
        "announced_at": None,
    }


async def _announce(client: httpx.AsyncClient, state: dict, first: bool) -> None:
    downloaded = fake_upload.bootstrap_downloaded_bytes(state["bootstrap_target"], state["first_download_at"])
    if FAKE_UPLOAD_MB_PER_HOUR > 0:
        state["uploaded_total"] += fake_upload.fake_upload_delta_bytes(state["infohash"], FAKE_UPLOAD_MB_PER_HOUR)

    info_hash = bytes.fromhex(state["infohash"])
    peer_id = bytes.fromhex(state["peer_id"])
    qs = (
        f"info_hash={quote(info_hash, safe='')}"
        f"&peer_id={quote(peer_id, safe='')}"
        f"&port={state['port']}&uploaded={state['uploaded_total']}&downloaded={downloaded}"
        f"&left=0&compact=1&numwant=0"
    )
    if first:
        qs += "&event=started"
    sep = "&" if "?" in state["announce_url"] else "?"
    url = state["announce_url"] + sep + qs

    try:
        r = await client.get(url, timeout=30)
        log.info("announced: downloaded=%s uploaded_total=%s -> HTTP %s", downloaded, state["uploaded_total"], r.status_code)
    except httpx.HTTPError as e:
        log.warning("announce failed: %s", e)
    state["announced_at"] = time.time()


async def main() -> None:
    if not (PORNOLAB_USERNAME and PORNOLAB_PASSWORD):
        raise SystemExit("PORNOLAB_USERNAME and PORNOLAB_PASSWORD are required")

    pl = Pornolab(PORNOLAB_URL, DATA_DIR / "fake_quota_cookies.json")
    state = _load_state()

    try:
        while True:
            _heartbeat()
            if state.get("skipped"):
                await asyncio.sleep(24 * 3600)
                continue

            if "topic_id" not in state:
                try:
                    update = await _bootstrap(pl, state)
                except PornolabError as e:
                    log.warning("bootstrap attempt failed: %s", e)
                    update = None
                if update:
                    state.update(update)
                    _save_state(state)
                if "topic_id" not in state:
                    await asyncio.sleep(BOOTSTRAP_CHECK_INTERVAL_S)
                    continue

            async with httpx.AsyncClient(headers={"User-Agent": UA}) as client:
                await _announce(client, state, first=state.get("announced_at") is None)
            _save_state(state)
            await asyncio.sleep(ANNOUNCE_INTERVAL_S)
    finally:
        await pl.close()


if __name__ == "__main__":
    asyncio.run(main())
