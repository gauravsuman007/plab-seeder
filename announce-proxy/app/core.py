"""Token registration and the announce relay.

A client (a seeder, the Prowlarr sidecar, ...) registers a tracker announce URL
and gets back a short random token and the proxy URL to put in the .torrent
instead. The real URL -- which carries the account's passkey -- never leaves
this container. When a torrent client announces to that proxy URL, the proxy
rewrites the counters (downloaded is reported as 0; uploaded is optionally
topped up with a steadily growing, irregular extra) and relays the announce to
the real tracker from the identity's own IP.
"""

import asyncio
import ipaddress
import re
import secrets
import socket
import time
from urllib.parse import urlparse

import httpx

from . import fakecredit
from .db import DB

# A long gap (proxy down, torrent paused) is credited as at most this much time,
# so catching up never lands as one huge spike.
MAX_CREDIT_GAP_S = 2 * 3600
DEFAULT_UA = "qBittorrent/4.6.0"

_credit_lock = asyncio.Lock()


def bencode_failure(msg: str) -> bytes:
    b = msg.encode()
    return b"d14:failure reason%d:%se" % (len(b), b)


async def check_target(url: str, settings: dict) -> str | None:
    """None if `url` is an acceptable tracker announce URL, else why not.

    Without this a registered URL could aim the proxy at anything on the
    network (it fetches that URL on every announce)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return "announce URL must be http(s)"
    host = p.hostname.lower()
    allowed = [h.strip().lower() for h in settings["allowed_hosts"].split(",") if h.strip()]
    if allowed and not any(host == a or host.endswith("." + a) for a in allowed):
        return f"host {host} is not in the allowed hosts list"
    if settings["allow_private_targets"]:
        return None
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror:
        return f"host {host} does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return f"host {host} resolves to a private address (enable 'allow private targets' to permit)"
    return None


async def register(db: DB, client: dict, *, url: str, label: str, announce_base: str,
                   first_download_at: float | None = None, bootstrap: bool = False,
                   passthrough: bool = False) -> dict:
    """Idempotent per (identity, url): the same URL always maps to the same token."""
    err = await check_target(url, db.settings())
    if err:
        raise ValueError(err)
    label = (label or "default").strip()[:80]
    ident = db.identity(client["id"], label)
    if first_download_at and not ident["first_download_at"]:
        db.execute("update identities set first_download_at=? where id=?", (float(first_download_at), ident["id"]))
        ident["first_download_at"] = float(first_download_at)

    row = db.one("select token from tokens where identity_id=? and real_url=?", (ident["id"], url))
    if row:
        token = row["token"]
    else:
        token = secrets.token_urlsafe(9)
        is_bootstrap = bool(bootstrap and not passthrough and not ident["first_download_at"])
        if is_bootstrap:
            db.execute("update identities set first_download_at=? where id=?", (time.time(), ident["id"]))
        db.execute(
            "insert into tokens(token, identity_id, real_url, passthrough, is_bootstrap, bootstrap_target,"
            " created_at) values (?,?,?,?,?,?,?)",
            (token, ident["id"], url, int(passthrough), int(is_bootstrap),
             fakecredit.bootstrap_target() if is_bootstrap else 0, time.time()))
    db.execute("update clients set last_seen=? where id=?", (time.time(), client["id"]))
    return {"token": token, "announce_url": f"{announce_base.rstrip('/')}/ann?t={token}"}


def _num(raw: str, name: str) -> tuple[int, bool]:
    m = re.search(rf"(?:^|&){name}=(\d+)", raw)
    return (int(m.group(1)), True) if m else (0, False)


def _set(raw: str, name: str, value: int, present: bool) -> str:
    if present:
        return re.sub(rf"(^|&){name}=\d+", rf"\g<1>{name}={value}", raw, count=1)
    return raw + f"&{name}={value}"


async def announce(db: DB, http: httpx.AsyncClient, *, token: str, query: str,
                   remote_ip: str, user_agent: str) -> bytes:
    t = db.one("select * from tokens where token=?", (token,))
    ident = db.one("select * from identities where id=?", (t["identity_id"],)) if t else None
    client = db.one("select * from clients where id=?", (ident["client_id"],)) if ident else None
    if not t or not client or not client["enabled"]:
        return bencode_failure("bad token")

    raw = re.sub(r"(^|&)t=[^&]*", "", query).lstrip("&")
    event = (re.search(r"(?:^|&)event=([a-z]+)", raw) or [None, ""])[1]
    real_up, _ = _num(raw, "uploaded")
    real_down, down_present = _num(raw, "downloaded")
    up_present = _num(raw, "uploaded")[1]

    # What the client sent since its last announce (a restart resets its counters).
    d_real_up = real_up - t["last_real_up"] if real_up >= t["last_real_up"] else real_up
    d_hidden_down = real_down - t["last_real_down"] if real_down >= t["last_real_down"] else real_down

    rep_up, rep_down, fake_added = real_up, real_down, 0
    if not t["passthrough"]:
        # downloaded: 0, except a brand-new account's first torrent ramps a fake total
        # over its first hour (see fakecredit.bootstrap_downloaded).
        rep_down = 0
        if (t["is_bootstrap"] and ident["first_download_at"]
                and time.time() - ident["first_download_at"] < fakecredit.BOOTSTRAP_WINDOW_S):
            rep_down = fakecredit.bootstrap_downloaded(t["bootstrap_target"], ident["first_download_at"])

        # uploaded: real + a persisted running offset that only ever grows, so the
        # tracker sees a counter that never goes backwards. The hourly rate is per
        # identity: time is measured from the identity's last credited announce
        # whichever torrent sent it, so N torrents share one rate.
        async with _credit_lock:
            ident = db.one("select * from identities where id=?", (ident["id"],))
            offset = t["fake_offset"]
            if ident["fake_upload"] and ident["fake_mb_per_hour"] > 0 and ident["first_download_at"]:
                now = time.time()
                last = ident["fake_last_at"]
                elapsed = min(now - last, MAX_CREDIT_GAP_S) if last else 1800
                offset += fakecredit.upload_delta_bytes(token, ident["fake_mb_per_hour"], max(elapsed, 0))
                db.execute("update identities set fake_last_at=? where id=?", (now, ident["id"]))
            rep_up = max(real_up + offset, t["last_reported_up"])
            fake_added = max(0, (rep_up - real_up) - t["fake_offset"])
            db.execute("update tokens set fake_offset=?, last_reported_up=? where token=?",
                       (rep_up - real_up, rep_up, token))
        raw = _set(raw, "downloaded", rep_down, down_present)
        raw = _set(raw, "uploaded", rep_up, up_present)

    db.execute("update tokens set last_real_up=?, last_real_down=?, announces=announces+1, last_announce_at=?"
               " where token=?", (real_up, real_down, time.time(), token))

    if ident["ip"] and not re.search(r"(?:^|&)ip=", raw):
        raw += f"&ip={ident['ip']}"
    url = t["real_url"] + ("&" if "?" in t["real_url"] else "?") + raw
    try:
        r = await http.get(url, headers={"User-Agent": user_agent or DEFAULT_UA, "Accept-Encoding": "gzip"})
        status, body = r.status_code, r.content
    except httpx.HTTPError:
        status, body = 0, bencode_failure("proxy error")

    db.record(client_id=client["id"], identity_id=ident["id"], remote_ip=remote_ip, user_agent=user_agent or "",
              event=event, real_up=real_up, rep_up=rep_up, real_down=real_down, rep_down=rep_down,
              status=status, d_real_up=d_real_up, d_fake_up=fake_added,
              d_hidden_down=0 if t["passthrough"] else d_hidden_down)
    return body
