"""Run with: pip install pytest && pytest announce-proxy/tests (from the repo root)."""

import asyncio
import sys
import time
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import announce_app, ui_app            # noqa: E402
from app.db import DB                           # noqa: E402

REAL = "http://tracker.example.test/ann?pk=SECRETKEY"


class Env:
    """An announce app + UI app over a temp DB, with a recording fake tracker."""

    def __init__(self, tmp_path):
        self.db = DB(tmp_path / "p.db")
        self.seen: list[dict] = []

        def tracker(request: httpx.Request) -> httpx.Response:
            q = dict(p.split("=", 1) for p in request.url.query.decode().split("&"))
            self.seen.append({**q, "_ua": request.headers.get("user-agent")})
            return httpx.Response(200, content=b"d8:intervali1800ee")

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(tracker))
        self.ann = httpx.AsyncClient(transport=httpx.ASGITransport(app=announce_app.make_app(self.db, self.http)),
                                     base_url="http://proxy.test:8101")
        self.ui = httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app.make_app(self.db)),
                                    base_url="http://ui.test")
        self.db.update_settings({"allow_private_targets": True})   # the fake tracker host doesn't resolve
        cid, self.key = self.db.create_client("seeder-a", "seeder")
        self.client_id = cid

    async def register(self, url=REAL, **kw):
        r = await self.ann.post("/v1/register", json={"url": url, "label": "acct1", **kw},
                                headers={"X-Api-Key": self.key})
        assert r.status_code == 200, r.text
        return r.json()

    async def announce(self, token, up=0, down=0, **extra):
        q = f"info_hash=%AB&peer_id=-qB4600-xxxxxxxxxxxx&port=6881&uploaded={up}&downloaded={down}&left=0"
        for k, v in extra.items():
            q += f"&{k}={v}"
        return await self.ann.get(f"/ann?t={token}&{q}", headers={"User-Agent": "qBittorrent/5.0.0"})


def run(coro_fn, tmp_path):
    async def go():
        env = Env(tmp_path)
        try:
            await coro_fn(env)
        finally:
            await env.http.aclose(); await env.ann.aclose(); await env.ui.aclose()
    asyncio.run(go())


def test_register_and_zero_downloaded(tmp_path):
    async def t(env):
        res = await env.register()
        assert res["announce_url"] == f"http://proxy.test:8101/ann?t={res['token']}"   # base derived from Host
        assert "SECRETKEY" not in res["announce_url"]
        r = await env.announce(res["token"], up=5000, down=777000)
        assert r.content == b"d8:intervali1800ee"
        seen = env.seen[-1]
        assert seen["downloaded"] == "0" and seen["uploaded"] == "5000" and seen["pk"] == "SECRETKEY"
        assert seen["_ua"] == "qBittorrent/5.0.0"          # the client's own UA is forwarded
        assert "t" not in seen
        again = await env.register()
        assert again["token"] == res["token"]               # idempotent per (identity, url)
    run(t, tmp_path)


def test_unknown_token_and_disabled_client(tmp_path):
    async def t(env):
        r = await env.announce("nope")
        assert b"bad token" in r.content
        res = await env.register()
        env.db.execute("update clients set enabled=0")
        assert b"bad token" in (await env.announce(res["token"])).content
        assert not env.seen
    run(t, tmp_path)


def test_register_auth_and_reverse_proxy_gate(tmp_path):
    async def t(env):
        body = {"url": REAL, "label": "a"}
        assert (await env.ann.post("/v1/register", json=body)).status_code == 404
        assert (await env.ann.post("/v1/register", json=body, headers={"X-Api-Key": "pap_wrong"})).status_code == 404
        h = {"X-Api-Key": env.key, "X-Forwarded-For": "203.0.113.9"}
        assert (await env.ann.post("/v1/register", json=body, headers=h)).status_code == 404   # came via a reverse proxy
        env.db.update_settings({"allow_remote_api": True})
        assert (await env.ann.post("/v1/register", json=body, headers=h)).status_code == 200
        for path in ("/", "/api/overview", "/api/settings", "/docs", "/openapi.json", "/healthz"):
            assert (await env.ann.get(path)).status_code == 404, path   # announce port serves nothing else
    run(t, tmp_path)


def test_failed_key_attempts_are_rate_limited(tmp_path):
    async def t(env):
        for _ in range(announce_app.FAIL_LIMIT):
            assert (await env.ann.get("/v1/whoami", headers={"X-Api-Key": "bad"})).status_code == 404
        assert (await env.ann.get("/v1/whoami", headers={"X-Api-Key": "bad"})).status_code == 429
        assert (await env.ann.get("/v1/whoami", headers={"X-Api-Key": env.key})).status_code == 429   # locked per IP
    run(t, tmp_path)


def test_private_targets_blocked_by_default(tmp_path):
    async def t(env):
        env.db.update_settings({"allow_private_targets": False})
        for url in ("http://127.0.0.1:9/ann", "http://192.168.1.5/ann", "ftp://x.test/ann", "http://localhost/ann"):
            r = await env.ann.post("/v1/register", json={"url": url, "label": "a"}, headers={"X-Api-Key": env.key})
            assert r.status_code == 400, url
        env.db.update_settings({"allowed_hosts": "pornolab.net", "allow_private_targets": True})
        r = await env.ann.post("/v1/register", json={"url": "http://evil.test/ann", "label": "a"},
                               headers={"X-Api-Key": env.key})
        assert r.status_code == 400 and "allowed" in r.json()["error"]
    run(t, tmp_path)


def test_announce_base_override_and_text_format(tmp_path):
    async def t(env):
        env.db.execute("update clients set announce_base='https://ann.example.org/'")
        res = await env.register()
        assert res["announce_url"].startswith("https://ann.example.org/ann?t=")
        r = await env.ann.post("/v1/register?format=text", data={"url": "http://tracker.example.test/x?pk=1", "label": "u"},
                               headers={"X-Api-Key": env.key})
        assert r.text.startswith("https://ann.example.org/ann?t=")          # form-encoded body, plain-text reply
    run(t, tmp_path)


def _identity(env):
    return env.db.one("select * from identities where label='acct1'")


def test_fake_upload_counter_never_goes_backwards_and_rate_is_shared(tmp_path):
    async def t(env):
        a = await env.register("http://tracker.example.test/a?pk=1", first_download_at=time.time() - 86400)
        b = await env.register("http://tracker.example.test/b?pk=1")
        env.db.execute("update identities set fake_upload=1, fake_mb_per_hour=250")
        clock = [time.time()]
        reported = {a["token"]: [], b["token"]: []}
        real = {a["token"]: 0, b["token"]: 0}
        with mock.patch("time.time", lambda: clock[0]):
            for i in range(48):                               # 24h, 30-min announces alternating torrents
                tok = a["token"] if i % 2 == 0 else b["token"]
                clock[0] += 1800
                real[tok] += 100_000                          # the client's own counter creeps up
                await env.announce(tok, up=real[tok])
                reported[tok].append(int(env.seen[-1]["uploaded"]))
        for tok, vals in reported.items():
            assert vals == sorted(vals), "counter went backwards"
        fake_total = sum(v[-1] - real[k] for k, v in reported.items())
        target = 250 * 24 * 1024 * 1024
        assert 0.8 * target < fake_total < 1.2 * target, (fake_total, target)   # one rate shared by both torrents
        assert len({v for vals in reported.values() for v in vals}) > 40         # no repeating values
        # switching it off freezes the offset: the counter must not drop back to the real value
        last = reported[a["token"]][-1]
        env.db.execute("update identities set fake_upload=0")
        await env.announce(a["token"], up=real[a["token"]])
        assert int(env.seen[-1]["uploaded"]) >= last
    run(t, tmp_path)


def test_fake_upload_held_back_without_download_history(tmp_path):
    async def t(env):
        a = await env.register()                              # no first_download_at, not a bootstrap
        env.db.execute("update identities set fake_upload=1")
        await env.announce(a["token"], up=1234)
        assert env.seen[-1]["uploaded"] == "1234"
    run(t, tmp_path)


def test_bootstrap_ramps_downloaded_then_freezes(tmp_path):
    async def t(env):
        a = await env.register(bootstrap=True)
        ident = _identity(env)
        assert ident["first_download_at"]
        target = env.db.one("select bootstrap_target from tokens")["bootstrap_target"]
        assert 0.9e9 <= target <= 1.1e9
        start = ident["first_download_at"]
        with mock.patch("time.time", lambda: start + 1800):
            await env.announce(a["token"], down=555)
            assert 0.3 * target < int(env.seen[-1]["downloaded"]) <= target
        with mock.patch("time.time", lambda: start + 7200):
            await env.announce(a["token"], down=555)
            assert env.seen[-1]["downloaded"] == "0"           # window over: back to 0
        b = await env.register("http://tracker.example.test/b?pk=2", bootstrap=True)
        assert env.db.one("select is_bootstrap from tokens where token=?", (b["token"],))["is_bootstrap"] == 0
    run(t, tmp_path)


def test_passthrough_leaves_counters_alone(tmp_path):
    async def t(env):
        a = await env.register(passthrough=True, first_download_at=time.time() - 9999)
        env.db.execute("update identities set fake_upload=1")
        await env.announce(a["token"], up=42, down=99)
        assert env.seen[-1]["uploaded"] == "42" and env.seen[-1]["downloaded"] == "99"
    run(t, tmp_path)


def test_identity_ip_is_sent_to_tracker(tmp_path):
    async def t(env):
        a = await env.register()
        r = await env.ann.post("/v1/identity", json={"label": "acct1", "ip": "198.51.100.7"}, headers={"X-Api-Key": env.key})
        assert r.status_code == 200
        await env.announce(a["token"])
        assert env.seen[-1]["ip"] == "198.51.100.7"
        await env.announce(a["token"], ip="203.0.113.1")        # a client-supplied ip= wins
        assert env.seen[-1]["ip"] == "203.0.113.1"
    run(t, tmp_path)


def test_stats_and_ui_api(tmp_path):
    async def t(env):
        a = await env.register(first_download_at=time.time() - 86400)
        env.db.execute("update identities set fake_upload=1")
        await env.announce(a["token"], up=1000, down=50_000, event="started")
        await env.announce(a["token"], up=3000, down=90_000)
        ov = (await env.ui.get("/api/overview")).json()
        c = ov["clients"][0]
        assert c["name"] == "seeder-a" and c["announces_24h"] == 2
        assert c["real_up_24h"] == 3000 and c["hidden_down_24h"] == 90_000 and c["fake_up_24h"] > 0
        assert c["identities"][0]["label"] == "acct1" and ov["totals"]["tokens"] == 1
        tr = (await env.ui.get("/api/trend?range=24h")).json()
        assert len(tr["series"][0]["points"]) == 24 and sum(p[1] for p in tr["series"][0]["points"]) == 2
        tr30 = (await env.ui.get("/api/trend?range=30d")).json()
        assert len(tr30["series"][0]["points"]) == 30
        act = (await env.ui.get("/api/activity")).json()
        assert act[0]["client"] == "seeder-a" and act[1]["event"] == "started"
        # the secret key never appears in any UI payload
        for path in ("/api/overview", "/api/activity", "/api/settings"):
            assert "SECRETKEY" not in (await env.ui.get(path)).text and env.key not in (await env.ui.get(path)).text
    run(t, tmp_path)


def test_ui_client_management(tmp_path):
    async def t(env):
        r = await env.ui.post("/api/clients", json={"name": "prowlarr", "kind": "prowlarr"})
        assert r.status_code == 200 and r.json()["key"].startswith("pap_")
        key, cid = r.json()["key"], r.json()["id"]
        assert (await env.ui.post("/api/clients", json={"name": "prowlarr"})).status_code == 409
        w = await env.ann.get("/v1/whoami", headers={"X-Api-Key": key})
        assert w.json()["client"] == "prowlarr"
        new = (await env.ui.post(f"/api/clients/{cid}/rotate-key")).json()["key"]
        assert (await env.ann.get("/v1/whoami", headers={"X-Api-Key": key})).status_code == 404
        assert (await env.ann.get("/v1/whoami", headers={"X-Api-Key": new})).status_code == 200
        await env.ui.patch(f"/api/clients/{cid}", json={"enabled": False})
        assert (await env.ann.get("/v1/whoami", headers={"X-Api-Key": new})).status_code == 404
        assert (await env.ui.delete(f"/api/clients/{cid}")).status_code == 200
        bad = await env.ui.patch("/api/identities/999", json={"fake_upload": True})
        assert bad.status_code == 404
    run(t, tmp_path)
