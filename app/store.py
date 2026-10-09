"""Settings (JSON) and history (SQLite), both under DATA_DIR."""

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))


@dataclass
class Settings:
    # qBittorrent (the vpngate container). Its WebUI whitelists the vpngate
    # docker subnet, so no credentials are needed from inside that network.
    qbit_url: str = os.environ.get("QBIT_URL", "http://gateway:8080")
    qbit_username: str = ""
    qbit_password: str = ""
    qbit_category: str = "pornolab"
    qbit_save_path: str = "/downloads/pornolab"      # as qBittorrent sees it
    gateway_url: str = os.environ.get("GATEWAY_URL", "http://gateway:8081")
    # The app's own reachable base URL, used to build the announce proxy address
    # that qBittorrent will hit.  Must be reachable from the qBittorrent container.
    self_url: str = os.environ.get("SELF_URL", "http://pornolab-seeder:8000")

    # PornoLab
    pornolab_url: str = "https://pornolab.net"
    pornolab_username: str = ""
    pornolab_password: str = ""                     # kept only if "remember" was ticked
    flaresolverr_url: str = ""                      # e.g. http://flaresolverr:8191

    # Automation
    auto: bool = False                              # off until the user turns it on
    daily_torrent_budget: int = 2                   # of the account's 5; Riven uses the rest
    max_active_downloads: int = 1
    scan_pages: int = 6                             # 50 releases a page
    scan_order: int = 11                            # tracker.php o= (11: leechers)
    scan_interval_min: int = 60
    profile_interval_min: int = 10

    # Selection
    min_leechers: int = 2
    max_seeders: int = 10
    min_size_mb: int = 20
    max_size_mb: int = 4096
    exclude_forums: list[int] = field(default_factory=list)

    # The download guard
    allow_crossing: bool = False                    # never cross 2 GB unless this is on
    ratio_target: float = 0.6
    overhead: float = 0.10
    guard_override: bool = False                    # ignore the guard entirely (ban risk; owner's call)

    # Pruning. Off by default: under never-cross the download budget is spent
    # once, so removing a torrent frees disk but never buys a replacement.
    prune: bool = False
    min_seed_hours: int = 72
    idle_hours: int = 48                            # no upload for this long -> removable
    disk_budget_gb: int = 50


SECRET_FIELDS = {"qbit_password", "pornolab_password"}


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "settings.json"
        self._lock = threading.Lock()
        self.settings = self._load()

    def _load(self) -> Settings:
        s = Settings()
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return s
        names = {f.name for f in fields(Settings)}
        for k, v in raw.items():
            if k in names:
                setattr(s, k, v)
        return s

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.settings), indent=2))
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)

    def update(self, values: dict) -> Settings:
        names = {f.name: f for f in fields(Settings)}
        for k, v in values.items():
            if k not in names:
                continue
            if k in SECRET_FIELDS and v in (None, "", "********"):
                continue        # the UI never receives secrets, so blank = unchanged
            current = getattr(self.settings, k)
            if isinstance(current, bool):
                v = bool(v)
            elif isinstance(current, int):
                v = int(v)
            elif isinstance(current, float):
                v = float(v)
            elif isinstance(current, list):
                v = [int(x) for x in (v if isinstance(v, list) else str(v).replace(",", " ").split())]
            setattr(self.settings, k, v)
        self.save()
        return self.settings

    def public(self) -> dict:
        d = asdict(self.settings)
        for k in SECRET_FIELDS:
            d[k] = "********" if d[k] else ""
        return d


SCHEMA = """
create table if not exists torrents (
    topic_id integer primary key,
    infohash text,
    title text,
    forum text,
    size integer,
    score real,
    seeders integer,
    leechers integer,
    added_at real,
    removed_at real,
    remove_reason text,
    last_uploaded integer default 0,
    last_upload_change real
);
create table if not exists fetches (
    id integer primary key autoincrement,
    at real,
    topic_id integer,
    ok integer,
    message text
);
create table if not exists snapshots (
    at real primary key,
    rating real,
    downloaded integer,
    credited_upload integer,
    today_down integer,
    today_up integer,
    qbit_uploaded integer
);
create table if not exists meta (
    key text primary key,
    value text
);
create table if not exists events (
    id integer primary key autoincrement,
    at real,
    level text,
    message text
);
"""


class DB:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "seeder.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def execute(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # meta (baselines)
    def meta(self, key: str) -> str | None:
        rows = self.rows("select value from meta where key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value) -> None:
        self.execute("insert or replace into meta(key, value) values (?,?)", (key, str(value)))

    def added_bytes(self) -> int:
        """Full size of every torrent ever added, removed ones included."""
        rows = self.rows("select coalesce(sum(size), 0) as s from torrents")
        return int(rows[0]["s"])

    # events
    def event(self, message: str, level: str = "info") -> None:
        self.execute("insert into events(at, level, message) values (?,?,?)", (time.time(), level, message))

    def events(self, limit: int = 200) -> list[dict]:
        return self.rows("select * from events order by id desc limit ?", (limit,))

    # .torrent fetches (the daily quota)
    def record_fetch(self, topic_id: int, ok: bool, message: str = "") -> None:
        self.execute("insert into fetches(at, topic_id, ok, message) values (?,?,?,?)",
                     (time.time(), topic_id, int(ok), message))

    def fetches_since(self, since: float) -> list[dict]:
        return self.rows("select * from fetches where at >= ? order by at", (since,))

    # torrents we added
    def known_topics(self) -> set[int]:
        return {r["topic_id"] for r in self.rows("select topic_id from torrents")}

    def torrents(self) -> list[dict]:
        return self.rows("select * from torrents order by added_at desc")

    def by_hash(self) -> dict[str, dict]:
        return {r["infohash"]: r for r in self.rows("select * from torrents where infohash is not null")}

    def add_torrent(self, **kw) -> None:
        cols = ",".join(kw)
        marks = ",".join("?" for _ in kw)
        self.execute(f"insert or replace into torrents({cols}) values ({marks})", tuple(kw.values()))

    def update_torrent(self, infohash: str, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        self.execute(f"update torrents set {sets} where infohash=?", (*kw.values(), infohash))

    # rating history
    def snapshot(self, **kw) -> None:
        cols = ",".join(kw)
        marks = ",".join("?" for _ in kw)
        self.execute(f"insert or replace into snapshots({cols}) values ({marks})", tuple(kw.values()))

    def history(self, since: float) -> list[dict]:
        return self.rows("select * from snapshots where at >= ? order by at", (since,))
