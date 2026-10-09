"""Torrent announce proxy — stdlib only, runs inside the Prowlarr container.

Endpoints:
  GET /dl/<topic_id>   fetch .torrent from PornoLab, rewrite announces, return bytes
  GET /ann?t=<token>…  forward tracker announce with downloaded=0
  GET /healthz         liveness check
"""
import hashlib, http.server, json, logging, os, re, secrets, socket, sys
import threading, time, urllib.parse, urllib.request
from pathlib import Path
from html.parser import HTMLParser

PORT          = int(os.environ.get("PROXY_PORT", "8008"))
BASE_URL      = os.environ.get("PORNOLAB_URL", "https://pornolab.net").rstrip("/")
USERNAME      = os.environ.get("PORNOLAB_USERNAME", "")
PASSWORD      = os.environ.get("PORNOLAB_PASSWORD", "")
SELF_URL      = os.environ.get("PROXY_SELF_URL", f"http://127.0.0.1:{PORT}")
DATA_DIR      = Path(os.environ.get("PROXY_DATA_DIR", "/config/proxy"))
COOKIE_FILE   = DATA_DIR / "cookies.json"
API_KEY       = os.environ.get("PROXY_API_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")

# ── session ──────────────────────────────────────────────────────────────────
_jar = urllib.request.HTTPCookieProcessor()
_opener = urllib.request.build_opener(_jar)
_session_lock = threading.Lock()
_logged_in = False


def _cp1251(s: str) -> bytes:
    return s.encode("cp1251", errors="replace")


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.inputs = {}
    def handle_starttag(self, tag, attrs):
        if tag == "input":
            d = dict(attrs); n = d.get("name",""); v = d.get("value","")
            if n: self.inputs[n] = v


def _login():
    global _logged_in
    if not USERNAME or not PASSWORD:
        raise RuntimeError("PORNOLAB_USERNAME / PORNOLAB_PASSWORD not set")
    req = urllib.request.Request(f"{BASE_URL}/forum/login.php",
                                 headers={"User-Agent": "Mozilla/5.0"})
    with _opener.open(req, timeout=20) as r:
        html = r.read().decode("cp1251", "replace")
    p = _FormParser(); p.feed(html)
    cap_sid = p.inputs.get("cap_sid", "")
    cap_code_name = next((k for k in p.inputs if k.startswith("cap_code_")), "cap_code_")
    fields = {
        "login_username": USERNAME, "login_password": PASSWORD,
        "login": "Вход", "cap_sid": cap_sid, cap_code_name: "",
    }
    body = "&".join(f"{k}={urllib.parse.quote(_cp1251(v).decode('latin-1'), safe='')}"
                    for k, v in fields.items()).encode()
    req2 = urllib.request.Request(
        f"{BASE_URL}/forum/login.php", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Referer": f"{BASE_URL}/forum/login.php",
                 "User-Agent": "Mozilla/5.0"})
    with _opener.open(req2, timeout=20) as r:
        r.read()
    _save_cookies()
    _logged_in = True
    log.info("logged in to PornoLab")


def _save_cookies():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cookies = {c.name: c.value for c in _jar.cookiejar}
    COOKIE_FILE.write_text(json.dumps(cookies))


def _load_cookies():
    try:
        for name, value in json.loads(COOKIE_FILE.read_text()).items():
            # inject into the jar via a fake response
            ck = urllib.request.http.cookiejar.Cookie(
                0, name, value, None, False,
                urllib.parse.urlparse(BASE_URL).netloc, False, False,
                "/", False, False, int(time.time()) + 86400 * 30,
                False, None, None, {})
            _jar.cookiejar.set_cookie(ck)
        return True
    except Exception:
        return False


def _fetch(url: str) -> bytes:
    global _logged_in
    with _session_lock:
        if not _logged_in:
            if not _load_cookies():
                _login()
            else:
                _logged_in = True
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": BASE_URL})
    with _opener.open(req, timeout=30) as r:
        return r.read()


# ── bencode ──────────────────────────────────────────────────────────────────
def _bdec(d: bytes, i: int):
    c = d[i:i+1]
    if c == b"i":
        e = d.index(b"e", i); return int(d[i+1:e]), e+1
    if c == b"l":
        i += 1; out = []
        while d[i:i+1] != b"e": v, i = _bdec(d, i); out.append(v)
        return out, i+1
    if c == b"d":
        i += 1; out = {}
        while d[i:i+1] != b"e":
            k, i = _bdec(d, i); v, i = _bdec(d, i); out[k] = v
        return out, i+1
    if c.isdigit():
        col = d.index(b":", i); n = int(d[i:col]); s = col+1
        return d[s:s+n], s+n
    raise ValueError(f"bad byte {c!r} at {i}")


def bdecode(d: bytes):
    v, end = _bdec(d, 0)
    if end != len(d): raise ValueError("trailing data")
    return v


def bencode(v) -> bytes:
    if isinstance(v, bytes): return str(len(v)).encode() + b":" + v
    if isinstance(v, str): b = v.encode(); return str(len(b)).encode() + b":" + b
    if isinstance(v, int): return b"i" + str(v).encode() + b"e"
    if isinstance(v, list): return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        return b"d" + b"".join(bencode(k)+bencode(v[k]) for k in sorted(v)) + b"e"
    raise ValueError(type(v))


def _info_span(d: bytes):
    i = 1
    while d[i:i+1] != b"e":
        k, i = _bdec(d, i); s = i; _, i = _bdec(d, i)
        if k == b"info": return s, i
    raise ValueError("no info dict")


# ── token store ───────────────────────────────────────────────────────────────
_tokens: dict[str, str] = {}


def _register(real_url: str) -> str:
    tok = secrets.token_urlsafe(9)
    _tokens[tok] = real_url
    return tok


def _rewrite_url(url: bytes) -> bytes:
    s = url.decode("utf-8", "replace")
    if not s.startswith(("http://", "https://")): return url
    return f"{SELF_URL.rstrip('/')}/ann?t={_register(s)}".encode()


def rewrite_announces(data: bytes) -> bytes:
    t = bdecode(data)
    if b"announce" in t:
        t[b"announce"] = _rewrite_url(t[b"announce"])
    if b"announce-list" in t:
        t[b"announce-list"] = [[_rewrite_url(u) for u in tier]
                               for tier in t[b"announce-list"]]
    start, end = _info_span(data)
    info_raw = data[start:end]
    t[b"info"] = b"__PH__"
    enc = bencode(t)
    needle = bencode(b"info") + bencode(b"__PH__")
    return enc.replace(needle, bencode(b"info") + info_raw, 1)


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "text/plain",
              headers: dict = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _check_key(self) -> bool:
        if not API_KEY:
            return True
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        if qs.get("apikey") == API_KEY:
            return True
        if self.headers.get("X-Api-Key") == API_KEY:
            return True
        self._send(403, b"forbidden"); return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/healthz":
            self._send(200, b"ok"); return

        if not self._check_key():
            return

        if path.startswith("/dl/"):
            self._handle_dl(path[4:]); return

        if path == "/ann":
            self._handle_ann(parsed.query); return

        self._send(404, b"not found")

    def _handle_dl(self, topic_id: str):
        if not topic_id.isdigit():
            self._send(400, b"bad topic id"); return
        tid = int(topic_id)
        try:
            raw = _fetch(f"{BASE_URL}/forum/dl.php?t={tid}")
        except Exception as e:
            log.warning("fetch error: %s", e)
            # session may have expired — try re-login once
            global _logged_in
            with _session_lock:
                _logged_in = False
                _jar.cookiejar.clear()
            try:
                raw = _fetch(f"{BASE_URL}/forum/dl.php?t={tid}")
            except Exception as e2:
                self._send(502, str(e2).encode()); return

        if raw[:1] != b"d" or b"4:info" not in raw[:4096]:
            code = 429 if b"limit" in raw[:512].lower() else 503
            self._send(code, b"PornoLab returned a page instead of a .torrent"); return
        try:
            data = rewrite_announces(raw)
        except Exception as e:
            self._send(502, f"torrent parse error: {e}".encode()); return

        self._send(200, data,
                   ctype="application/x-bittorrent",
                   headers={"Content-Disposition":
                            f'attachment; filename="{tid}.torrent"'})

    def _handle_ann(self, query: str):
        qs = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        tok = qs.get("t", "")
        real = _tokens.get(tok)
        if not real:
            self._send(200, b"d14:failure reason9:bad tokene"); return

        # strip our token param, clamp downloaded=0
        raw = re.sub(r"(^|&)t=[^&]*", "", query).lstrip("&")
        raw = re.sub(r"(^|&)downloaded=\d+", r"\1downloaded=0", raw)
        url = real + ("&" if "?" in real else "?") + raw
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
            self._send(200, body)
        except Exception as e:
            log.warning("announce proxy error: %s", e)
            self._send(200, b"d14:failure reason11:proxy errore")


# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("torrent-proxy listening on port %d", PORT)
    server.serve_forever()
