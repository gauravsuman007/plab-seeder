"""Management UI and admin API (UI_PORT). Keep this port private."""

import base64
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ANNOUNCE_PORT, UI_PASSWORD, UI_PORT
import httpx

from . import core
from .db import DB, normalize_public_url

HERE = Path(__file__).parent
KINDS = ("seeder", "fake-quota", "prowlarr", "other")
HAIRPIN_NOTE = ("This test runs from the proxy host itself; without hairpin NAT on your router it can fail "
                "here even though the address works from outside.")
RANGES = {"24h": (24, 3600), "7d": (7 * 24, 3600), "30d": (30 * 24, 86400), "90d": (90 * 24, 86400)}


def make_app(db: DB, http: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(title="announce-proxy", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if UI_PASSWORD and request.url.path != "/healthz":
            header = request.headers.get("authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(pw, UI_PASSWORD)
                except ValueError:
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="announce-proxy"'})
        return await call_next(request)

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    @app.get("/")
    async def index():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/info")
    async def info():
        return {"announce_port": ANNOUNCE_PORT, "ui_port": UI_PORT}

    # ---- dashboard --------------------------------------------------------
    @app.get("/api/overview")
    async def overview():
        now = time.time()
        since = int(now // 3600) - 23
        usage = {r["client_id"]: r for r in db.rows(
            "select client_id, sum(announces) a, sum(errors) e, sum(real_up) ru, sum(fake_up) fu,"
            " sum(hidden_down) hd from hourly where hour >= ? group by client_id", (since,))}
        total = {r["client_id"]: r for r in db.rows(
            "select client_id, sum(announces) a, sum(fake_up) fu, sum(real_up) ru, sum(hidden_down) hd"
            " from hourly group by client_id")}
        clients = []
        for c in db.rows("select id, name, kind, enabled, announce_base, use_public, created_at, last_seen from clients order by name"):
            u, tt = usage.get(c["id"], {}), total.get(c["id"], {})
            idents = db.rows(
                "select i.id, i.label, i.ip, i.fake_upload, i.fake_mb_per_hour, i.first_download_at, i.last_seen,"
                " (select count(*) from tokens t where t.identity_id=i.id) tokens,"
                " (select coalesce(sum(announces),0) from hourly h where h.identity_id=i.id and h.hour>=?) a24,"
                " (select coalesce(sum(fake_up),0) from hourly h where h.identity_id=i.id and h.hour>=?) fu24"
                " from identities i where i.client_id=? order by i.label", (since, since, c["id"]))
            recent_ips = [r["remote_ip"] for r in db.rows(
                "select distinct remote_ip from recent where client_id=? and at>? order by at desc limit 5",
                (c["id"], now - 86400))]
            clients.append({
                **c, "identities": idents, "source_ips": recent_ips,
                "announces_24h": u.get("a") or 0, "errors_24h": u.get("e") or 0,
                "real_up_24h": u.get("ru") or 0, "fake_up_24h": u.get("fu") or 0,
                "hidden_down_24h": u.get("hd") or 0,
                "announces_total": tt.get("a") or 0, "fake_up_total": tt.get("fu") or 0,
                "real_up_total": tt.get("ru") or 0, "hidden_down_total": tt.get("hd") or 0,
            })
        return {
            "now": now, "clients": clients,
            "totals": {
                "clients": len(clients),
                "active_24h": sum(1 for c in clients if c["announces_24h"]),
                "tokens": db.one("select count(*) n from tokens")["n"],
                "announces_24h": sum(c["announces_24h"] for c in clients),
                "errors_24h": sum(c["errors_24h"] for c in clients),
                "fake_up_24h": sum(c["fake_up_24h"] for c in clients),
                "hidden_down_24h": sum(c["hidden_down_24h"] for c in clients),
            },
        }

    @app.get("/api/trend")
    async def trend(rng: str = Query("7d", alias="range")):
        hours, bucket = RANGES.get(rng, RANGES["7d"])
        now_hour = int(time.time() // 3600)
        first_hour = now_hour - hours + 1
        step = bucket // 3600
        n = (now_hour - first_hour) // step + 1
        series = []
        for c in db.rows("select id, name from clients order by name"):
            pts = [[(first_hour + i * step) * 3600, 0, 0, 0, 0, 0] for i in range(n)]
            for r in db.rows(
                    "select hour, announces, errors, real_up, fake_up, hidden_down from hourly"
                    " where client_id=? and hour>=?", (c["id"], first_hour)):
                i = (r["hour"] - first_hour) // step
                p = pts[min(i, n - 1)]
                p[1] += r["announces"]; p[2] += r["errors"]; p[3] += r["real_up"]
                p[4] += r["fake_up"]; p[5] += r["hidden_down"]
            series.append({"client_id": c["id"], "name": c["name"], "points": pts})
        return {"range": rng, "bucket_s": bucket, "fields": ["t", "announces", "errors", "real_up", "fake_up", "hidden_down"],
                "series": series}

    @app.get("/api/activity")
    async def activity(limit: int = 100):
        return db.rows(
            "select r.at, c.name client, i.label identity, r.remote_ip, r.user_agent, r.event, r.real_up,"
            " r.rep_up, r.real_down, r.rep_down, r.status from recent r"
            " left join clients c on c.id=r.client_id left join identities i on i.id=r.identity_id"
            " order by r.id desc limit ?", (max(1, min(limit, 500)),))

    # ---- clients ----------------------------------------------------------
    @app.post("/api/clients")
    async def create_client(request: Request):
        b = await body(request)
        name = str(b.get("name") or "").strip()[:60]
        kind = b.get("kind") if b.get("kind") in KINDS else "other"
        if not name:
            raise HTTPException(400, "name is required")
        if db.one("select 1 from clients where name=?", (name,)):
            raise HTTPException(409, "a client with that name exists")
        cid, key = db.create_client(name, kind)
        return {"id": cid, "name": name, "kind": kind, "key": key}      # the only time the key is shown

    @app.patch("/api/clients/{cid}")
    async def patch_client(cid: int, request: Request):
        b = await body(request)
        if not db.one("select 1 from clients where id=?", (cid,)):
            raise HTTPException(404)
        if "enabled" in b:
            db.execute("update clients set enabled=? where id=?", (int(bool(b["enabled"])), cid))
        if "announce_base" in b:
            db.execute("update clients set announce_base=? where id=?", (str(b["announce_base"]).strip().rstrip("/"), cid))
        if "use_public" in b:
            db.execute("update clients set use_public=? where id=?", (int(bool(b["use_public"])), cid))
        if "kind" in b and b["kind"] in KINDS:
            db.execute("update clients set kind=? where id=?", (b["kind"], cid))
        return {"ok": True}

    @app.post("/api/clients/{cid}/rotate-key")
    async def rotate(cid: int):
        if not db.one("select 1 from clients where id=?", (cid,)):
            raise HTTPException(404)
        return {"key": db.rotate_key(cid)}

    @app.delete("/api/clients/{cid}")
    async def delete_client(cid: int):
        ids = [r["id"] for r in db.rows("select id from identities where client_id=?", (cid,))]
        for iid in ids:
            db.execute("delete from tokens where identity_id=?", (iid,))
        db.execute("delete from identities where client_id=?", (cid,))
        db.execute("delete from hourly where client_id=?", (cid,))
        db.execute("delete from recent where client_id=?", (cid,))
        db.execute("delete from clients where id=?", (cid,))
        return {"ok": True}

    # ---- identities -------------------------------------------------------
    @app.patch("/api/identities/{iid}")
    async def patch_identity(iid: int, request: Request):
        b = await body(request)
        if not db.one("select 1 from identities where id=?", (iid,)):
            raise HTTPException(404)
        if "fake_upload" in b:
            db.execute("update identities set fake_upload=? where id=?", (int(bool(b["fake_upload"])), iid))
        if "fake_mb_per_hour" in b:
            try:
                mb = int(b["fake_mb_per_hour"])
            except (TypeError, ValueError):
                raise HTTPException(400, "fake_mb_per_hour must be a number")
            if not 1 <= mb <= 100_000:
                raise HTTPException(400, "fake_mb_per_hour must be between 1 and 100000")
            db.execute("update identities set fake_mb_per_hour=? where id=?", (mb, iid))
        return {"ok": True}

    # ---- settings ---------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings():
        return db.settings()

    @app.post("/api/settings")
    async def set_settings(request: Request):
        try:
            return db.update_settings(await body(request))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/public-url/test")
    async def test_public_url(request: Request):
        """Check, without saving anything, that an address really reaches this proxy's
        announce port: ask it (from here) for a one-shot probe token and look for our nonce."""
        try:
            base = normalize_public_url((await body(request)).get("url"))
        except ValueError as e:
            return {"ok": False, "detail": str(e)}
        if not base:
            return {"ok": False, "detail": "Enter the address your reverse proxy serves, e.g. https://ann.example.com"}
        nonce = core.new_probe()
        client = http or httpx.AsyncClient(timeout=10)
        try:
            r = await client.get(f"{base}/ann?t=probe-{nonce}", timeout=10, headers={"User-Agent": "announce-proxy-probe"})
        except httpx.ConnectTimeout:
            return {"ok": False, "detail": f"Timed out connecting to {base}. " + HAIRPIN_NOTE}
        except httpx.ConnectError as e:
            msg = str(e)
            if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper():
                return {"ok": False, "detail": "Reached the server but its TLS certificate isn't valid for that domain."}
            if any(w in msg.lower() for w in ("name or service", "nodename", "resolve", "getaddrinfo")):
                return {"ok": False, "detail": "That domain doesn't resolve from the proxy's host (check the DNS record)."}
            return {"ok": False, "detail": f"Couldn't connect to {base}. " + HAIRPIN_NOTE}
        except httpx.HTTPError as e:
            return {"ok": False, "detail": f"Request failed ({type(e).__name__}). " + HAIRPIN_NOTE}
        finally:
            if http is None:
                await client.aclose()
        if b"probe " + nonce.encode() in r.content:
            return {"ok": True, "detail": f"{base} reaches this proxy's announce port."}
        if r.status_code in (301, 302, 307, 308):
            return {"ok": False, "detail": f"{base} redirects to {r.headers.get('location', 'elsewhere')} - enter that address instead."}
        if r.status_code in (502, 503, 504):
            return {"ok": False, "detail": "The reverse proxy answered but couldn't reach the announce port - check its upstream (port and host)."}
        return {"ok": False, "detail": f"{base} answered HTTP {r.status_code}, but not from this proxy - "
                                       "check the reverse proxy forwards to the announce port, not the UI port."}

    return app
