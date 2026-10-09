"""Standalone torrent proxy for private trackers.

Fetches .torrent files, rewrites their announce URLs through a local
no-download-reporting proxy, and returns the modified bytes.  Designed
to be the download target for a Prowlarr custom indexer so any downstream
app (Radarr, Sonarr, qBittorrent) gets a torrent that never reports
downloaded bytes back to the tracker.

Two endpoints:
  GET /dl/{topic_id}   — fetch + rewrite + return .torrent
  GET /ann             — announce proxy: forwards to tracker with downloaded=0
"""
import hashlib
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
PORNOLAB_URL  = os.environ.get("PORNOLAB_URL", "https://pornolab.net")
USERNAME      = os.environ.get("PORNOLAB_USERNAME", "")
PASSWORD      = os.environ.get("PORNOLAB_PASSWORD", "")
SELF_URL      = os.environ.get("SELF_URL", "http://torrent-proxy:8000")
COOKIE_FILE   = Path(os.environ.get("DATA_DIR", "/data")) / "proxy_cookies.json"
API_KEY       = os.environ.get("API_KEY", "")          # optional; set to require auth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("proxy")

app = FastAPI(title="torrent-proxy")

# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------
_cookies: dict[str, str] = {}
_last_login: float = 0.0


def _load_cookies() -> None:
    global _cookies
    try:
        import json
        _cookies = json.loads(COOKIE_FILE.read_text())
    except Exception:
        _cookies = {}


def _save_cookies() -> None:
    import json
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(_cookies))


async def _login() -> None:
    global _cookies, _last_login
    if not USERNAME or not PASSWORD:
        raise RuntimeError("PORNOLAB_USERNAME / PORNOLAB_PASSWORD not set")

    from urllib.parse import quote_plus
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as c:
        # fetch login page to get captcha sid
        r = await c.get(f"{PORNOLAB_URL}/forum/login.php")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.content, "html.parser")
        cap_sid_el = soup.find("input", {"name": "cap_sid"})
        cap_sid = cap_sid_el["value"] if cap_sid_el else ""
        cap_code_el = soup.find("input", attrs={"name": re.compile(r"cap_code_")})
        cap_code_name = cap_code_el["name"] if cap_code_el else "cap_code_"

        payload = {
            "login_username": USERNAME.encode("cp1251"),
            "login_password": PASSWORD.encode("cp1251"),
            "login": quote_plus("Вход".encode("cp1251")),
            "cap_sid": cap_sid,
            cap_code_name: "",
        }
        r2 = await c.post(
            f"{PORNOLAB_URL}/forum/login.php",
            data=payload,
            headers={"Referer": f"{PORNOLAB_URL}/forum/login.php"},
        )
        _cookies = dict(c.cookies)
        _save_cookies()
        _last_login = time.time()
        log.info("logged in to PornoLab")


async def _get_session() -> httpx.AsyncClient:
    _load_cookies()
    if not _cookies and (not _last_login or time.time() - _last_login > 3600):
        await _login()
    client = httpx.AsyncClient(
        cookies=_cookies,
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
        timeout=30,
    )
    return client


# ---------------------------------------------------------------------------
# bencode — enough to rewrite announce URLs and preserve the info hash
# ---------------------------------------------------------------------------
def _bdecode(data: bytes, i: int):
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        i += 1; out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i); out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1; out = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i); v, i = _bdecode(data, i); out[k] = v
        return out, i + 1
    if c.isdigit():
        colon = data.index(b":", i); n = int(data[i:colon])
        s = colon + 1; return data[s:s + n], s + n
    raise ValueError(f"unexpected byte {c!r} at {i}")


def bdecode(data: bytes):
    v, end = _bdecode(data, 0)
    if end != len(data):
        raise ValueError("trailing data")
    return v


def _bencode(v) -> bytes:
    if isinstance(v, bytes):
        return str(len(v)).encode() + b":" + v
    if isinstance(v, str):
        b = v.encode(); return str(len(b)).encode() + b":" + b
    if isinstance(v, int):
        return b"i" + str(v).encode() + b"e"
    if isinstance(v, list):
        return b"l" + b"".join(_bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        out = b"d"
        for k in sorted(v.keys()):
            out += _bencode(k) + _bencode(v[k])
        return out + b"e"
    raise ValueError(f"cannot encode {type(v)}")


def _info_span(data: bytes) -> tuple[int, int]:
    if data[:1] != b"d":
        raise ValueError("not a dict")
    i = 1
    while data[i:i + 1] != b"e":
        k, i = _bdecode(data, i); start = i; _, i = _bdecode(data, i)
        if k == b"info":
            return start, i
    raise ValueError("no info dict")


# ---------------------------------------------------------------------------
# token store — maps short token -> real tracker URL
# ---------------------------------------------------------------------------
_tokens: dict[str, str] = {}


def _register(real_url: str) -> str:
    tok = secrets.token_urlsafe(9)
    _tokens[tok] = real_url
    return tok


def _rewrite_url(url: bytes) -> bytes:
    s = url.decode("utf-8", "replace")
    if not s.startswith(("http://", "https://")):
        return url
    tok = _register(s)
    return f"{SELF_URL.rstrip('/')}/ann?t={tok}".encode()


def rewrite_announces(data: bytes) -> bytes:
    t = bdecode(data)
    if not isinstance(t, dict):
        raise ValueError("not a dict")
    if b"announce" in t:
        t[b"announce"] = _rewrite_url(t[b"announce"])
    if b"announce-list" in t:
        t[b"announce-list"] = [[_rewrite_url(u) for u in tier]
                               for tier in t[b"announce-list"]]
    start, end = _info_span(data)
    info_bytes = data[start:end]
    t[b"info"] = b"__PH__"
    encoded = _bencode(t)
    needle = _bencode(b"info") + _bencode(b"__PH__")
    return encoded.replace(needle, _bencode(b"info") + info_bytes, 1)


# ---------------------------------------------------------------------------
# auth middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def check_key(request: Request, call_next):
    if API_KEY and request.url.path not in ("/healthz",):
        if request.query_params.get("apikey") != API_KEY and \
           request.headers.get("X-Api-Key") != API_KEY:
            return Response(status_code=403, content="forbidden")
    return await call_next(request)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/dl/{topic_id}")
async def download(topic_id: int):
    """Fetch .torrent from PornoLab, rewrite announces, return modified bytes."""
    client = await _get_session()
    try:
        r = await client.get(
            f"{PORNOLAB_URL}/forum/dl.php?t={topic_id}",
            headers={"Referer": f"{PORNOLAB_URL}/forum/viewtopic.php?t={topic_id}"},
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, str(e))
    finally:
        await client.aclose()

    if r.content[:1] != b"d" or b"4:info" not in r.content[:4096]:
        # site returned an HTML page — not logged in or daily limit hit
        status = 429 if b"\xeb\xe5" in r.content[:512] else 503
        raise HTTPException(status, "PornoLab returned a page instead of a .torrent")

    try:
        data = rewrite_announces(r.content)
    except Exception as e:
        raise HTTPException(502, f"torrent parse error: {e}")

    return Response(
        content=data,
        media_type="application/x-bittorrent",
        headers={"Content-Disposition": f'attachment; filename="{topic_id}.torrent"'},
    )


@app.get("/ann")
async def announce_proxy(request: Request):
    """Forward tracker announce with downloaded=0."""
    tok = request.query_params.get("t", "")
    real = _tokens.get(tok)
    if not real:
        return Response(b"d14:failure reason9:bad tokene", media_type="text/plain")

    # pass query string through as-is except override downloaded and inject ip
    raw = re.sub(r"(^|&)t=[^&]*", "", request.url.query).lstrip("&")
    raw = re.sub(r"(^|&)downloaded=\d+", r"\1downloaded=0", raw)
    url = real + ("&" if "?" in real else "?") + raw
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url)
        return Response(content=r.content, media_type="text/plain")
    except Exception as e:
        log.warning("announce proxy error: %s", e)
        return Response(b"d14:failure reason11:proxy errore", media_type="text/plain")
