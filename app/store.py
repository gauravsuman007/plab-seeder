"""Settings (JSON + per-account SQLite rows) and history (SQLite), all under DATA_DIR.

Multi-account model: a small set of fields are genuinely shared infrastructure
(the qBittorrent connection, the gateway, this app's own URL, the email-factory
config) and stay in one global settings.json, managed by GlobalSettingsStore.
Everything else — PornoLab credentials, scan/selection rules, the download
guard, pruning, fake-upload-credit — is a property of one PornoLab account and
lives as a JSON blob on that account's row in the `accounts` table, managed by
AccountStore.

Engine (app/engine.py) was written against a single flat Settings object with
every field and a `store.update(...)` / `store.settings` interface. Rather than
rewrite its internals, AccountSettingsHandle presents that same interface for
one account by merging the global fields with that account's own — so
`self.s.overhead`, `self.s.qbit_url`, `self.s.auto` etc. all keep working
unchanged regardless of which store they actually come from. AccountDB does
the equivalent for the DB class: it binds one account_id so `self.db.event(...)`
etc. don't need an account_id passed at every call site.
"""

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))


@dataclass
class GlobalSettings:
    """Shared infrastructure, one copy regardless of how many accounts exist."""
    # qBittorrent (the vpngate container). Its WebUI whitelists the vpngate
    # docker subnet, so no credentials are needed from inside that network.
    qbit_url: str = os.environ.get("QBIT_URL", "http://gateway:8080")
    qbit_username: str = ""
    qbit_password: str = ""
    gateway_url: str = os.environ.get("GATEWAY_URL", "http://gateway:8081")
    # The app's own reachable base URL, used to build the announce proxy address
    # that qBittorrent will hit.  Must be reachable from the qBittorrent container.
    self_url: str = os.environ.get("SELF_URL", "http://pornolab-seeder:8000")

    pornolab_url: str = "https://pornolab.net"

    # Where the plab-register deployment lives, for the "create a new
    # account" link -- registration/email-factory automation isn't part of
    # this app any more (see plab-register), this just points at it.
    register_url: str = os.environ.get("REGISTER_URL", "")

    # Public announce listener: a second, announce-only HTTP server so a
    # reverse proxy can expose /proxy/ann to the internet (e.g. for a debrid
    # service that must reach the rewritten announce URL) without exposing
    # the dashboard. The port is the one the listener binds *inside* the
    # container; docker-compose publishes the matching host port.
    public_announce_enabled: bool = os.environ.get("PUBLIC_ANNOUNCE_ENABLED", "").lower() in ("1", "true", "yes")
    public_announce_port: int = int(os.environ.get("ANNOUNCE_PORT", "8001"))
    public_announce_url: str = os.environ.get("PUBLIC_ANNOUNCE_URL", "")   # what the reverse proxy serves, e.g. https://ann.example.com


@dataclass
class AccountSettings:
    """Per-PornoLab-account settings: credentials, scan/selection rules, guard."""
    pornolab_username: str = ""
    pornolab_password: str = ""                     # kept only if "remember" was ticked
    qbit_category: str = "pornolab"
    qbit_save_path: str = "/downloads/pornolab"      # as qBittorrent sees it

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

    # Fake upload credit (off by default — ban risk; owner's call).
    # Each announce injects a randomised delta so the tracker sees growing
    # upload even when qBittorrent isn't seeding.  The daily total averages
    # within ±10 % of fake_upload_mb_per_hour × 24.  The variance is wide and
    # intentionally irregular so announce-to-announce deltas look like real
    # bursty traffic rather than a synthetic clock signal. Gated on the account
    # having a real first-download bootstrap — see Engine/announce_proxy.
    fake_upload_credit: bool = False
    fake_upload_mb_per_hour: int = 250

    # Fake download credit (off by default — ban risk; owner's call).
    # Same variance model as fake_upload_credit but accumulates the downloaded
    # counter. Default ≈ 1 GiB/day (43 MB/hr × 24). Added on top of the
    # one-time bootstrap ramp so the total keeps growing organically.
    fake_download_credit: bool = False
    fake_download_mb_per_hour: int = 43


# A merged view Engine actually reads: every field either store contributes.
# Field-identical in spirit to the old single Settings dataclass.
@dataclass
class Settings(GlobalSettings, AccountSettings):
    pass


GLOBAL_SECRET_FIELDS = {"qbit_password"}
ACCOUNT_SECRET_FIELDS = {"pornolab_password"}
SECRET_FIELDS = GLOBAL_SECRET_FIELDS | ACCOUNT_SECRET_FIELDS     # kept for callers that don't care which


class GlobalSettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "settings.json"
        self._lock = threading.Lock()
        self.settings = self._load()

    def _load(self) -> GlobalSettings:
        s = GlobalSettings()
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return s
        names = {f.name for f in fields(GlobalSettings)}
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

    def update(self, values: dict) -> GlobalSettings:
        names = {f.name: f for f in fields(GlobalSettings)}
        for k, v in values.items():
            if k not in names:
                continue
            if k in GLOBAL_SECRET_FIELDS and v in (None, "", "********"):
                continue        # the UI never receives secrets, so blank = unchanged
            current = getattr(self.settings, k)
            v = _coerce(current, v)
            setattr(self.settings, k, v)
        self.save()
        return self.settings

    def public(self) -> dict:
        d = asdict(self.settings)
        for k in GLOBAL_SECRET_FIELDS:
            d[k] = "********" if d[k] else ""
        return d


def _coerce(current, v):
    if isinstance(current, bool):
        return bool(v)
    if isinstance(current, int):
        return int(v)
    if isinstance(current, float):
        return float(v)
    if isinstance(current, list):
        return [int(x) for x in (v if isinstance(v, list) else str(v).replace(",", " ").split())]
    return v


SCHEMA = """
create table if not exists accounts (
    id integer primary key autoincrement,
    label text,
    created_at real,
    settings_json text,
    email_address text,
    email_password text,
    email_provider text,
    first_download_at real,
    first_download_token text
);
create table if not exists torrents (
    account_id integer not null default 1,
    topic_id integer,
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
    last_upload_change real,
    primary key (account_id, topic_id)
);
create table if not exists fetches (
    id integer primary key autoincrement,
    account_id integer not null default 1,
    at real,
    topic_id integer,
    ok integer,
    message text
);
create table if not exists snapshots (
    account_id integer not null default 1,
    at real,
    rating real,
    downloaded integer,
    credited_upload integer,
    today_down integer,
    today_up integer,
    qbit_uploaded integer,
    primary key (account_id, at)
);
create table if not exists meta (
    account_id integer not null default 1,
    key text,
    value text,
    primary key (account_id, key)
);
create table if not exists events (
    id integer primary key autoincrement,
    account_id integer not null default 1,
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
        self._lock = threading.Lock()
        self._migrate()

    def _migrate(self) -> None:
        """Create the schema, then add account_id to any pre-multi-account
        tables that don't have it yet (a deployment upgrading in place)."""
        with self._lock:
            self.conn.executescript(SCHEMA)
            for table in ("torrents", "fetches", "snapshots", "meta", "events"):
                cols = {r["name"] for r in self.conn.execute(f"pragma table_info({table})")}
                if "account_id" not in cols:
                    self.conn.execute(f"alter table {table} add column account_id integer not null default 1")
            # The standalone "emails" registry moved to plab-register along
            # with the rest of the registration/email-factory flow -- drop
            # it on upgrade rather than carrying a dead table forever.
            # Confirmed empty in production before this migration shipped.
            self.conn.execute("drop table if exists emails")
            self.conn.commit()

    def execute(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # -- accounts ------------------------------------------------------- #
    def list_accounts(self) -> list[dict]:
        return self.rows("select id, label, created_at, email_address, email_provider, "
                         "first_download_at from accounts order by id")

    def get_account(self, account_id: int) -> dict | None:
        rows = self.rows("select * from accounts where id=?", (account_id,))
        return rows[0] if rows else None

    def create_account(self, *, label: str, settings: dict, email_address: str = "",
                       email_password: str = "", email_provider: str = "") -> int:
        cur = self.execute(
            "insert into accounts(label, created_at, settings_json, email_address, "
            "email_password, email_provider) values (?,?,?,?,?,?)",
            (label, time.time(), json.dumps(settings), email_address, email_password, email_provider),
        )
        return cur.lastrowid

    def update_account_settings(self, account_id: int, settings: dict) -> None:
        self.execute("update accounts set settings_json=? where id=?", (json.dumps(settings), account_id))

    def set_account_first_download(self, account_id: int, token: str) -> None:
        self.execute("update accounts set first_download_at=?, first_download_token=? where id=?",
                     (time.time(), token, account_id))

    # -- meta (baselines) ------------------------------------------------ #
    def meta(self, account_id: int, key: str) -> str | None:
        rows = self.rows("select value from meta where account_id=? and key=?", (account_id, key))
        return rows[0]["value"] if rows else None

    def set_meta(self, account_id: int, key: str, value) -> None:
        self.execute("insert or replace into meta(account_id, key, value) values (?,?,?)",
                     (account_id, key, str(value)))

    def added_bytes(self, account_id: int) -> int:
        """Full size of every torrent ever added for this account, removed ones included."""
        rows = self.rows("select coalesce(sum(size), 0) as s from torrents where account_id=?", (account_id,))
        return int(rows[0]["s"])

    # -- events ----------------------------------------------------------- #
    def event(self, account_id: int, message: str, level: str = "info") -> None:
        self.execute("insert into events(account_id, at, level, message) values (?,?,?,?)",
                     (account_id, time.time(), level, message))

    def events(self, account_id: int, limit: int = 200) -> list[dict]:
        return self.rows("select * from events where account_id=? order by id desc limit ?",
                         (account_id, limit))

    # -- .torrent fetches (the daily quota) ------------------------------- #
    def record_fetch(self, account_id: int, topic_id: int, ok: bool, message: str = "") -> None:
        self.execute("insert into fetches(account_id, at, topic_id, ok, message) values (?,?,?,?,?)",
                     (account_id, time.time(), topic_id, int(ok), message))

    def fetches_since(self, account_id: int, since: float) -> list[dict]:
        return self.rows("select * from fetches where account_id=? and at >= ? order by at",
                         (account_id, since))

    # -- torrents we added ------------------------------------------------ #
    def known_topics(self, account_id: int) -> set[int]:
        return {r["topic_id"] for r in self.rows("select topic_id from torrents where account_id=?", (account_id,))}

    def torrents(self, account_id: int) -> list[dict]:
        return self.rows("select * from torrents where account_id=? order by added_at desc", (account_id,))

    def by_hash(self, account_id: int) -> dict[str, dict]:
        return {r["infohash"]: r for r in
                self.rows("select * from torrents where account_id=? and infohash is not null", (account_id,))}

    def add_torrent(self, account_id: int, **kw) -> None:
        kw["account_id"] = account_id
        cols = ",".join(kw)
        marks = ",".join("?" for _ in kw)
        self.execute(f"insert or replace into torrents({cols}) values ({marks})", tuple(kw.values()))

    def update_torrent(self, account_id: int, infohash: str, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        self.execute(f"update torrents set {sets} where account_id=? and infohash=?",
                     (*kw.values(), account_id, infohash))

    # -- rating history ----------------------------------------------------- #
    def snapshot(self, account_id: int, **kw) -> None:
        kw["account_id"] = account_id
        cols = ",".join(kw)
        marks = ",".join("?" for _ in kw)
        self.execute(f"insert or replace into snapshots({cols}) values ({marks})", tuple(kw.values()))

    def history(self, account_id: int, since: float) -> list[dict]:
        return self.rows("select * from snapshots where account_id=? and at >= ? order by at",
                         (account_id, since))

    # -- announce-token -> account resolution (used by the proxy route) -- #
    def account_for_token(self, token: str) -> int | None:
        rows = self.rows("select account_id from meta where key=?", (f"ann:{token}",))
        return rows[0]["account_id"] if rows else None


def migrate_legacy_single_account(db: DB, global_store: "GlobalSettingsStore") -> int:
    """First run after upgrading from the single-account version: if there are
    no accounts yet but an old-style settings.json carries PornoLab creds and
    per-account fields, create account id=1 from it so the existing deployment
    keeps its history (the account_id-default-1 migration in DB._migrate
    already backfilled every pre-existing row to account_id=1).

    Returns the id of the account to treat as "the" account for legacy routes
    that haven't been made account-aware yet (always 1 once this has run).
    """
    existing = db.list_accounts()
    if existing:
        return existing[0]["id"]

    raw = {}
    try:
        raw = json.loads(global_store.path.read_text())
    except (OSError, ValueError):
        pass
    account_field_names = {f.name for f in fields(AccountSettings)}
    settings = asdict(AccountSettings())
    for k, v in raw.items():
        if k in account_field_names:
            settings[k] = v
    label = settings.get("pornolab_username") or "account 1"
    account_id = db.create_account(label=label, settings=settings)
    assert account_id == 1
    return account_id


class AccountStore:
    """Per-account settings, backed by the `accounts` table."""

    def __init__(self, db: DB):
        self.db = db

    def get_settings(self, account_id: int) -> dict:
        row = self.db.get_account(account_id)
        if not row:
            raise KeyError(f"no such account: {account_id}")
        return json.loads(row["settings_json"] or "{}")

    def update_settings(self, account_id: int, values: dict) -> dict:
        current = self.get_settings(account_id)
        defaults = asdict(AccountSettings())
        names = set(defaults)
        for k, v in values.items():
            if k not in names:
                continue
            if k in ACCOUNT_SECRET_FIELDS and v in (None, "", "********"):
                continue
            current_val = current.get(k, defaults[k])
            current[k] = _coerce(current_val, v)
        self.db.update_account_settings(account_id, current)
        return current

    def public(self, account_id: int) -> dict:
        d = {**asdict(AccountSettings()), **self.get_settings(account_id)}
        for k in ACCOUNT_SECRET_FIELDS:
            d[k] = "********" if d[k] else ""
        return d


class AccountSettingsHandle:
    """Presents the old single-SettingsStore interface (`.settings`, `.update()`,
    `.save()`) for one account, merging global + that account's own fields, so
    Engine's existing `self.s.<field>` / `self.store.update(...)` calls work
    unchanged regardless of which underlying store a field actually lives in."""

    def __init__(self, global_store: GlobalSettingsStore, account_store: AccountStore, account_id: int):
        self._global = global_store
        self._accounts = account_store
        self.account_id = account_id

    @property
    def settings(self) -> Settings:
        merged = {**asdict(self._global.settings), **self._accounts.get_settings(self.account_id)}
        names = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in merged.items() if k in names})

    def update(self, values: dict) -> Settings:
        global_names = {f.name for f in fields(GlobalSettings)}
        g_vals = {k: v for k, v in values.items() if k in global_names}
        a_vals = {k: v for k, v in values.items() if k not in global_names}
        if g_vals:
            self._global.update(g_vals)
        if a_vals:
            self._accounts.update_settings(self.account_id, a_vals)
        return self.settings

    def save(self) -> None:
        pass  # updates above persist immediately; kept only for interface parity


class AccountDB:
    """Binds a DB instance to one account_id so Engine's `self.db.<method>(...)`
    calls don't need an account_id threaded through every call site."""

    def __init__(self, db: DB, account_id: int):
        self._db = db
        self.account_id = account_id

    def meta(self, key: str) -> str | None:
        return self._db.meta(self.account_id, key)

    def set_meta(self, key: str, value) -> None:
        return self._db.set_meta(self.account_id, key, value)

    def added_bytes(self) -> int:
        return self._db.added_bytes(self.account_id)

    def event(self, message: str, level: str = "info") -> None:
        return self._db.event(self.account_id, message, level)

    def events(self, limit: int = 200) -> list[dict]:
        return self._db.events(self.account_id, limit)

    def record_fetch(self, topic_id: int, ok: bool, message: str = "") -> None:
        return self._db.record_fetch(self.account_id, topic_id, ok, message)

    def fetches_since(self, since: float) -> list[dict]:
        return self._db.fetches_since(self.account_id, since)

    def known_topics(self) -> set[int]:
        return self._db.known_topics(self.account_id)

    def torrents(self) -> list[dict]:
        return self._db.torrents(self.account_id)

    def by_hash(self) -> dict[str, dict]:
        return self._db.by_hash(self.account_id)

    def add_torrent(self, **kw) -> None:
        return self._db.add_torrent(self.account_id, **kw)

    def update_torrent(self, infohash: str, **kw) -> None:
        return self._db.update_torrent(self.account_id, infohash, **kw)

    def snapshot(self, **kw) -> None:
        return self._db.snapshot(self.account_id, **kw)

    def history(self, since: float) -> list[dict]:
        return self._db.history(self.account_id, since)

    def set_first_download(self, token: str) -> None:
        return self._db.set_account_first_download(self.account_id, token)

    def has_first_download(self) -> bool:
        row = self._db.get_account(self.account_id)
        return bool(row and row["first_download_at"])
