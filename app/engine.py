"""The seeder: background loops, the download guard, and the actions behind the UI."""

import asyncio
import logging
import secrets
import time

import httpx

from . import limits, strategy
from .bencode import BencodeError, torrent_meta, redirect_announce
from .parsing import Profile, Release
from .pornolab import CaptchaRequired, LimitReached, NotLoggedIn, Pornolab, PornolabError
from .qbit import Qbit, QbitError
from .store import DB, SettingsStore

log = logging.getLogger("seeder")

DAY = 24 * 3600
QBIT_INTERVAL = 10
DECIDE_INTERVAL = 300

# Ratio-aware pacing for auto-seed while the guard override is on (see
# Engine._auto_pacing): starts around here while the ratio is near zero and
# grows toward "generous" as credited upload catches up with ratio_target.
# 1.25 GiB comfortably covers a ~1 GB release even after the overhead charge.
RECOVERY_MIN_BYTES = int(1.25 * 1024**3)
RECOVERY_MAX_BYTES = 4 * 1024**3


class Engine:
    def __init__(self, store: SettingsStore, db: DB):
        self.store = store
        self.db = db
        s = store.settings
        self.pl = Pornolab(s.pornolab_url)
        self.qb = Qbit(s.qbit_url, s.qbit_username, s.qbit_password)
        self.tasks: list[asyncio.Task] = []
        self._decide_lock = asyncio.Lock()
        self._last_error: dict[str, str] = {}

        self.profile: Profile | None = None
        self.profile_at: float | None = None
        self.logged_in = False
        self.qbit: dict = {"ok": False}
        self.gateway: dict = {"ok": False}
        self.torrents: list[dict] = []
        self.candidates: list[strategy.Candidate] = []
        self.releases: dict[int, Release] = {}
        self.scan_at: float | None = None
        self.scan_stats: dict = {}
        self.limit_hit_at: float | None = None

    @property
    def s(self):
        return self.store.settings

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        for name, coro in (("qbit", self._qbit_loop), ("profile", self._profile_loop),
                           ("scan", self._scan_loop), ("decide", self._decide_loop)):
            self.tasks.append(asyncio.create_task(self._forever(name, coro), name=name))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await self.pl.close()
        await self.qb.close()

    async def _forever(self, name: str, coro) -> None:
        while True:
            try:
                delay = await coro()
                self._last_error.pop(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 -- a loop must never die
                msg = f"{name}: {e}"
                if self._last_error.get(name) != msg:   # log each distinct failure once
                    self.db.event(msg, "error")
                    log.exception(msg)
                self._last_error[name] = msg
                delay = 60
            await asyncio.sleep(delay)

    def reconfigure(self) -> None:
        """Rebuild clients after settings changed."""
        s = self.s
        old_qb, old_pl = self.qb, self.pl
        self.qb = Qbit(s.qbit_url, s.qbit_username, s.qbit_password)
        if old_pl.base != s.pornolab_url.rstrip("/"):
            self.pl = Pornolab(s.pornolab_url)
            asyncio.create_task(old_pl.close())
        asyncio.create_task(old_qb.close())

    # ------------------------------------------------------------------ #
    # the guard
    # ------------------------------------------------------------------ #
    def pending_bytes(self) -> int:
        """Still to download from our torrents that are actually running.

        A torrent the guard stopped is not coming back on its own, so it no
        longer counts as pending (the ledger still charges its full size).
        """
        return sum(int(t.get("amount_left") or 0) for t in self.torrents
                   if (t.get("progress") or 0) < 1 and not str(t.get("state", "")).startswith(("stopped", "paused")))

    def ledger_bytes(self) -> int:
        baseline = int(self.db.meta("baseline_downloaded") or 0)
        return baseline + int(self.db.added_bytes() * (1 + self.s.overhead))

    def guard(self) -> limits.Guard | None:
        if not self.profile:
            return None
        return limits.guard(self.profile, self.pending_bytes(), self.ledger_bytes(),
                            allow_crossing=self.s.allow_crossing,
                            ratio_target=self.s.ratio_target, overhead=self.s.overhead,
                            override=self.s.guard_override)

    def budget(self) -> dict:
        """The seeder's share of PornoLab's daily .torrent quota."""
        now = time.time()
        fetches = self.db.fetches_since(now - DAY)
        recent = [f for f in fetches if f["ok"]]      # a refused fetch downloaded nothing
        used = len(recent)
        left = max(0, self.s.daily_torrent_budget - used)
        next_slot = recent[0]["at"] + DAY if recent and left == 0 else None
        refused_until = None
        # Read from the DB so a restart doesn't forget a refusal.
        refusals = [f["at"] for f in fetches if not f["ok"]]
        hit = max(refusals + ([self.limit_hit_at] if self.limit_hit_at else []), default=None)
        if hit and now - hit < DAY:
            refused_until = hit + DAY
            left = 0
        return {"budget": self.s.daily_torrent_budget, "used": used, "left": left,
                "next_slot": next_slot, "refused_until": refused_until}

    # ------------------------------------------------------------------ #
    # loops (each returns the seconds until its next run)
    # ------------------------------------------------------------------ #
    async def _qbit_loop(self) -> float:
        try:
            version = await self.qb.version()
            transfer = await self.qb.transfer()
            torrents = await self.qb.torrents(self.s.qbit_category)
            self.qbit = {"ok": True, "version": version, "transfer": transfer, "at": time.time()}
            self.torrents = torrents
        except QbitError as e:
            self.qbit = {"ok": False, "error": str(e), "at": time.time()}
            self.torrents = []

        try:
            async with httpx.AsyncClient(timeout=5) as c:
                st = (await c.get(f"{self.s.gateway_url.rstrip('/')}/api/status")).json()
            self.gateway = {"ok": True, "active": st.get("active_tunnels"), "desired": st.get("desired_tunnels"),
                            "mbps": st.get("agg_mbps"), "proto": st.get("proto")}
        except Exception as e:  # noqa: BLE001 -- the gateway panel is informational
            self.gateway = {"ok": False, "error": str(e)}

        self._track_uploads()
        await self._enforce_guard()
        return QBIT_INTERVAL

    def _track_uploads(self) -> None:
        known = self.db.by_hash()
        now = time.time()
        for t in self.torrents:
            row = known.get(t["hash"])
            if not row:
                continue
            up = int(t.get("uploaded") or 0)
            if up != (row["last_uploaded"] or 0):
                self.db.update_torrent(t["hash"], last_uploaded=up, last_upload_change=now)

    async def _enforce_guard(self) -> None:
        """Backstop: stop downloading torrents if the profile says we're too close.

        New torrents are only added when they fit, so this fires only on
        surprises: heavy re-downloads (hash failures) or downloads made outside
        the seeder that ate the headroom.
        """
        g = self.guard()
        if not g or g.mode != "never-cross":
            return
        over = g.live_downloaded + g.pending - g.ceiling
        downloading = [t for t in self.torrents if (t.get("progress") or 0) < 1
                       and not str(t.get("state", "")).startswith(("stopped", "paused"))]
        runaway = [t for t in downloading
                   if int(t.get("downloaded") or 0) > int(t.get("size") or 0) * (1 + self.s.overhead)]
        victims = downloading if over > 0 else runaway
        if not victims:
            return
        await self.qb.stop([t["hash"] for t in victims])
        for t in victims:
            self.db.event(f"Guard stopped '{t['name']}': the 2 GB line was too close", "warning")

    async def _profile_loop(self) -> float:
        await self.refresh_profile()
        return self.s.profile_interval_min * 60

    async def refresh_profile(self) -> None:
        try:
            self.profile = await self.pl.profile()
        except NotLoggedIn:
            self.logged_in = False
            await self._relogin()
            if not self.logged_in:
                return
            self.profile = await self.pl.profile()
        self.logged_in = True
        self.profile_at = time.time()

        p = self.profile
        if self.db.meta("baseline_downloaded") is None:
            # Everything before this point is the profile's business; from here
            # on the ledger adds what the seeder itself fetches.
            self.db.set_meta("baseline_downloaded", p.live_downloaded)
            self.db.set_meta("baseline_upload", p.live_credited_upload)
            self.db.set_meta("baseline_at", time.time())
        self.db.snapshot(at=time.time(), rating=p.rating, downloaded=p.live_downloaded,
                         credited_upload=p.live_credited_upload, today_down=p.down.today,
                         today_up=p.up.today + p.own.today + p.bonus.today,
                         qbit_uploaded=sum(int(t.get("uploaded") or 0) for t in self.torrents))

    async def _relogin(self) -> None:
        s = self.s
        if not (s.pornolab_username and s.pornolab_password):
            return
        try:
            await self.pl.login(s.pornolab_username, s.pornolab_password)
            self.logged_in = True
            self.db.event("Logged back in to PornoLab")
        except CaptchaRequired:
            self.db.event("PornoLab wants a captcha: log in again from the Settings tab", "warning")
        except PornolabError as e:
            self.db.event(f"PornoLab login failed: {e}", "error")

    async def _scan_loop(self) -> float:
        if self.logged_in:
            await self.scan()
        return self.s.scan_interval_min * 60 if self.logged_in else 120

    async def scan(self) -> None:
        releases: list[Release] = []
        for page in range(self.s.scan_pages):
            try:
                releases += await self.pl.tracker_page(page, self.s.scan_order)
            except NotLoggedIn:
                self.logged_in = False
                return
        self.releases = {r.topic_id: r for r in releases}
        self.scan_at = time.time()
        self._rerank()
        self.scan_stats = {"releases": len(releases),
                           "candidates": sum(1 for c in self.candidates if c.blocked is None),
                           "blocked_by_guard": sum(1 for c in self.candidates if c.blocked == "would cross the download guard")}

    def _rules(self) -> strategy.Rules:
        s = self.s
        return strategy.Rules(min_leechers=s.min_leechers, max_seeders=s.max_seeders,
                              min_size_mb=s.min_size_mb, max_size_mb=s.max_size_mb,
                              exclude_forums=tuple(s.exclude_forums))

    def _rerank(self) -> None:
        g = self.guard()
        if not g:
            self.candidates = []
            return
        self.candidates = strategy.rank(list(self.releases.values()), self._rules(), g, self.db.known_topics())

    async def _decide_loop(self) -> float:
        await self.decide()
        return DECIDE_INTERVAL

    async def decide(self) -> None:
        async with self._decide_lock:
            if self.s.prune and self.qbit.get("ok"):
                await self._prune()
            self._rerank()
            if not (self.s.auto and self.logged_in and self.qbit.get("ok") and self.profile):
                return
            if not self.profile_at or time.time() - self.profile_at > 3 * self.s.profile_interval_min * 60:
                return              # never decide on stale numbers
            active = sum(1 for t in self.torrents if (t.get("progress") or 0) < 1)
            slots = min(self.budget()["left"], max(0, self.s.max_active_downloads - active))
            if slots <= 0:
                return
            g = self.guard()
            headroom, size_bias = self._auto_pacing(g)
            for c in strategy.pick(self.candidates, headroom, slots, size_bias=size_bias):
                try:
                    await self.add(c.release.topic_id, reason="auto")
                except PornolabError:
                    break           # quota or session problem: stop for this round

    def _auto_pacing(self, g: limits.Guard) -> tuple[int, float]:
        """How hard auto-seed should hold back while the ratio is still poor.

        Below ratio_target, prefer smaller files over the raw best score (so a
        thin budget buys several small releases instead of one big one that
        eats it all) -- the more the ratio lags, the stronger that pull.

        Outside override, the guard's own headroom (0 in never-cross once
        over the line, a ratio-scaled figure in ratio mode) already keeps
        bytes in check, so only override -- whose headroom is otherwise
        unbounded -- gets an additional self-imposed cap here, growing from
        RECOVERY_MIN_BYTES to RECOVERY_MAX_BYTES as the ratio recovers.
        """
        target = self.s.ratio_target
        health = min(1.0, g.ratio / target) if target > 0 else 1.0
        if health >= 1.0:
            return g.headroom, 0.0
        headroom = g.headroom
        if g.mode == "override":
            cap = int(RECOVERY_MIN_BYTES + health * (RECOVERY_MAX_BYTES - RECOVERY_MIN_BYTES))
            headroom = min(headroom, cap)
        return headroom, 2.0 * (1 - health)

    def _candidate_dicts(self, g: limits.Guard | None) -> list[dict]:
        """The Candidates tab's view: each release plus whether auto-seed would
        actually grab it right now -- which can differ from `fits` (the raw
        guard check manual Add uses) once pacing is holding bigger files back.
        """
        headroom, _ = self._auto_pacing(g) if g else (0, 0.0)
        out = []
        for c in self.candidates[:150]:
            d = c.to_dict()
            auto_fits = bool(c.fits and c.cost <= headroom)
            d["auto_fits"] = auto_fits
            d["auto_reason"] = (None if auto_fits or not c.fits else
                                f"fits, but over auto-seed's pacing cap ({limits.fmt(headroom)} right now)")
            out.append(d)
        return out

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    async def add(self, topic_id: int, reason: str = "manual") -> dict:
        """Fetch a topic's .torrent and hand it to qBittorrent -- if the guard allows."""
        release = self.releases.get(topic_id)
        if not release:
            raise ValueError("unknown topic: rescan first")
        if topic_id in self.db.known_topics():
            raise ValueError("already added")
        if not self.profile:
            raise ValueError("no PornoLab profile yet: log in first")
        if self.budget()["left"] <= 0:
            raise ValueError("today's .torrent budget is used up")
        g = self.guard()
        if not g.fits(release.size):
            raise ValueError(f"would cross the download guard: costs {limits.fmt(g.cost(release.size))}, "
                             f"{limits.fmt(g.headroom)} left")

        try:
            data = await self.pl.download(topic_id)
        except LimitReached as e:
            self.limit_hit_at = time.time()
            self.db.record_fetch(topic_id, False, str(e))
            self.db.event(f"PornoLab refused the .torrent for {topic_id}: {e}", "warning")
            raise
        self.db.record_fetch(topic_id, True)

        try:
            data = redirect_announce(data, self.s.self_url, self.register_announce)
            meta = torrent_meta(data)
        except BencodeError as e:
            raise ValueError(f"PornoLab sent an unreadable .torrent: {e}") from e

        # The listing's size is rounded; the .torrent's is exact. Check again.
        if not self.guard().fits(meta["size"]):
            self.db.event(f"Not adding '{release.title}': its exact size ({limits.fmt(meta['size'])}) "
                          f"no longer fits the guard", "warning")
            raise ValueError("exact size does not fit the guard")

        await self.qb.ensure_category(self.s.qbit_category, self.s.qbit_save_path)
        await self.qb.add(data, f"{topic_id}.torrent", self.s.qbit_category, self.s.qbit_save_path)
        self.db.add_torrent(topic_id=topic_id, infohash=meta["infohash"], title=release.title,
                            forum=release.forum, size=meta["size"], score=strategy.score(release),
                            seeders=release.seeders, leechers=release.leechers, added_at=time.time(),
                            last_uploaded=0, last_upload_change=time.time())
        self.db.event(f"Added ({reason}) '{release.title}' — {limits.fmt(meta['size'])}, "
                      f"{release.seeders} seeders / {release.leechers} leechers")
        self._rerank()
        return meta

    async def remove(self, infohash: str, delete_files: bool = True, reason: str = "manual") -> None:
        await self.qb.delete([infohash], delete_files)
        self.db.update_torrent(infohash, removed_at=time.time(), remove_reason=reason)
        self.db.event(f"Removed {infohash[:8]} ({reason})")

    async def _prune(self) -> None:
        known = self.db.by_hash()
        now = time.time()
        seeding = [t for t in self.torrents if t["hash"] in known and (t.get("progress") or 0) >= 1]
        for t in seeding:
            row = known[t["hash"]]
            seeded = int(t.get("seeding_time") or 0)
            idle = now - (row["last_upload_change"] or row["added_at"] or now)
            if seeded >= self.s.min_seed_hours * 3600 and idle >= self.s.idle_hours * 3600:
                await self.remove(t["hash"], True, f"no upload for {self.s.idle_hours}h")

        total = sum(int(t.get("size") or 0) for t in self.torrents)
        budget = self.s.disk_budget_gb * 1024**3
        if total > budget:
            # Over the disk budget: drop the slowest earners that have done their minimum.
            def per_day(t):
                return int(t.get("uploaded") or 0) / max(1, int(t.get("seeding_time") or 1)) * DAY
            for t in sorted(seeding, key=per_day):
                if total <= budget:
                    break
                if int(t.get("seeding_time") or 0) >= self.s.min_seed_hours * 3600:
                    await self.remove(t["hash"], True, "over the disk budget")
                    total -= int(t.get("size") or 0)

    async def login(self, username: str, password: str, remember: bool, captcha: str | None) -> dict:
        try:
            await self.pl.login(username, password, captcha)
        except CaptchaRequired as e:
            return {"ok": False, "captcha": e.image}
        except PornolabError as e:
            return {"ok": False, "error": str(e)}
        self.store.update({"pornolab_username": username,
                           "pornolab_password": password if remember else ""})
        if not remember:
            self.store.settings.pornolab_password = ""
            self.store.save()
        self.logged_in = True
        self.db.event(f"Logged in to PornoLab as {username}")
        await self.refresh_profile()
        asyncio.create_task(self.scan())
        return {"ok": True}

    def logout(self) -> None:
        self.pl.clear_session()
        self.logged_in = False
        self.profile = None
        self.store.update({"pornolab_password": ""})
        self.store.settings.pornolab_password = ""
        self.store.save()
        self.db.event("Logged out of PornoLab")

    # ------------------------------------------------------------------ #
    # the UI's view
    # ------------------------------------------------------------------ #
    def register_announce(self, url: str) -> str:
        token = secrets.token_urlsafe(9)
        self.db.set_meta(f"ann:{token}", url)
        return token

    async def proxy_existing(self, infohash: str) -> int:
        """Swap a torrent's real tracker URLs for proxy ones (already-added torrents)."""
        n = 0
        for t in await self.qb.trackers(infohash):
            u = t.get("url", "")
            if u.startswith(("http://", "https://")) and "/proxy/ann?t=" not in u:
                new = f"{self.s.self_url.rstrip('/')}/proxy/ann?t={self.register_announce(u)}"
                await self.qb.replace_tracker(infohash, u, new)
                n += 1
        return n

    def state(self) -> dict:
        p = self.profile
        g = self.guard()
        tier = limits.tier(p) if p else None
        known = self.db.by_hash()
        baseline_up = int(self.db.meta("baseline_upload") or 0)

        torrents = []
        for t in self.torrents:
            row = known.get(t["hash"], {})
            torrents.append({
                "hash": t["hash"], "name": t.get("name"), "size": t.get("size"),
                "progress": t.get("progress"), "state": t.get("state"),
                "dlspeed": t.get("dlspeed"), "upspeed": t.get("upspeed"),
                "downloaded": t.get("downloaded"), "uploaded": t.get("uploaded"),
                "ratio": t.get("ratio"), "num_seeds": t.get("num_seeds"), "num_leechs": t.get("num_leechs"),
                "num_complete": t.get("num_complete"), "num_incomplete": t.get("num_incomplete"),
                "seeding_time": t.get("seeding_time"), "eta": t.get("eta"), "added_on": t.get("added_on"),
                "topic_id": row.get("topic_id"), "tracker_msg": t.get("tracker"),
                "last_upload_change": row.get("last_upload_change"),
            })

        return {
            "now": time.time(),
            "auto": self.s.auto,
            "guard_override": self.s.guard_override,
            "logged_in": self.logged_in,
            "username": self.s.pornolab_username or None,
            "pending_captcha": self.pl.pending_captcha is not None,
            "profile": None if not p else {
                "at": self.profile_at, "username": p.username, "rating": p.rating, "newbie": p.newbie,
                "downloaded": p.live_downloaded, "credited_upload": p.live_credited_upload,
                "uploaded": p.uploaded, "uploaded_own": p.uploaded_own, "uploaded_bonus": p.uploaded_bonus,
                "today": {"down": p.down.today, "up": p.up.today, "own": p.own.today, "bonus": p.bonus.today},
                "yesterday": {"down": p.down.yesterday, "up": p.up.yesterday, "own": p.own.yesterday,
                              "bonus": p.bonus.yesterday},
                "effective_ratio": (p.live_credited_upload / p.live_downloaded) if p.live_downloaded else None,
                "credited_since_start": max(0, p.live_credited_upload - baseline_up),
            },
            "tier": None if not tier else {"name": tier.name, "daily": tier.daily_torrents, "note": tier.note},
            "guard": None if not g else {
                "mode": g.mode, "ceiling": g.ceiling, "live_downloaded": g.live_downloaded,
                "pending": g.pending, "ledger": g.ledger, "committed": g.committed,
                "headroom": g.headroom, "overhead": g.overhead, "reason": g.reason, "ratio": g.ratio,
            },
            "budget": self.budget(),
            "qbit": self.qbit,
            "gateway": self.gateway,
            "torrents": torrents,
            "qbit_uploaded_total": sum(int(t.get("uploaded") or 0) for t in self.torrents),
            "seeding_bytes": sum(int(t.get("size") or 0) for t in self.torrents),
            "candidates": self._candidate_dicts(g),
            "scan": {"at": self.scan_at, **self.scan_stats},
            "errors": dict(self._last_error),
        }
