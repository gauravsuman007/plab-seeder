"""SQLite state: clients, identities (one PornoLab account each), announce
tokens, hourly rollups and a short recent-activity log."""

import hashlib
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from .config import DATA_DIR

SCHEMA = """
create table if not exists clients (
    id integer primary key autoincrement,
    name text unique not null,
    kind text not null default 'other',
    key_hash text not null,
    announce_base text not null default '',
    enabled integer not null default 1,
    created_at real,
    last_seen real
);
create table if not exists identities (
    id integer primary key autoincrement,
    client_id integer not null,
    label text not null,
    ip text not null default '',
    first_download_at real,
    fake_upload integer not null default 0,
    fake_mb_per_hour integer not null default 250,
    fake_last_at real not null default 0,
    created_at real,
    last_seen real,
    unique(client_id, label)
);
create table if not exists tokens (
    token text primary key,
    identity_id integer not null,
    real_url text not null,
    passthrough integer not null default 0,
    is_bootstrap integer not null default 0,
    bootstrap_target integer not null default 0,
    created_at real,
    last_announce_at real,
    announces integer not null default 0,
    last_real_up integer not null default 0,
    last_real_down integer not null default 0,
    fake_offset integer not null default 0,
    last_reported_up integer not null default 0
);
create index if not exists tokens_identity on tokens(identity_id);
create table if not exists hourly (
    client_id integer not null,
    identity_id integer not null,
    hour integer not null,
    announces integer not null default 0,
    errors integer not null default 0,
    real_up integer not null default 0,
    fake_up integer not null default 0,
    hidden_down integer not null default 0,
    primary key (client_id, identity_id, hour)
);
create table if not exists recent (
    id integer primary key autoincrement,
    at real,
    client_id integer,
    identity_id integer,
    remote_ip text,
    user_agent text,
    event text,
    real_up integer,
    rep_up integer,
    real_down integer,
    rep_down integer,
    status integer
);
create index if not exists recent_at on recent(at);
create table if not exists settings (
    key text primary key,
    value text
);
"""

# name -> (default, type). Edited from the UI; all stored as text.
SETTING_DEFAULTS = {
    "allow_remote_api": (False, bool),      # accept /v1/* through a reverse proxy (X-Forwarded-For present)
    "allow_private_targets": (False, bool), # let tokens point at private/loopback tracker addresses
    "allowed_hosts": ("", str),             # comma-separated; empty = any public host
    "retention_days": (30, int),            # recent-activity log; hourly rollups are kept 400 days
}
HOURLY_KEEP_DAYS = 400


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return "pap_" + secrets.token_urlsafe(24)


class DB:
    def __init__(self, path=None):
        self.path = Path(path) if path else DATA_DIR / "proxy.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
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

    def one(self, sql: str, args=()) -> dict | None:
        r = self.rows(sql, args)
        return r[0] if r else None

    # ---- settings -------------------------------------------------------- #
    def settings(self) -> dict:
        stored = {r["key"]: r["value"] for r in self.rows("select key, value from settings")}
        out = {}
        for k, (default, typ) in SETTING_DEFAULTS.items():
            if k not in stored:
                out[k] = default
            elif typ is bool:
                out[k] = stored[k] == "1"
            elif typ is int:
                out[k] = int(stored[k])
            else:
                out[k] = stored[k]
        return out

    def update_settings(self, values: dict) -> dict:
        for k, v in values.items():
            if k not in SETTING_DEFAULTS:
                continue
            typ = SETTING_DEFAULTS[k][1]
            if typ is bool:
                v = "1" if v in (True, 1, "1", "true", "on") else "0"
            elif typ is int:
                v = str(max(1, int(v)))
            else:
                v = str(v).strip()
            self.execute("insert or replace into settings(key, value) values (?,?)", (k, v))
        return self.settings()

    # ---- clients --------------------------------------------------------- #
    def create_client(self, name: str, kind: str = "other") -> tuple[int, str]:
        key = new_key()
        cur = self.execute(
            "insert into clients(name, kind, key_hash, created_at) values (?,?,?,?)",
            (name, kind, hash_key(key), time.time()))
        return cur.lastrowid, key

    def client_by_key(self, key: str) -> dict | None:
        if not key:
            return None
        return self.one("select * from clients where key_hash=?", (hash_key(key),))

    def rotate_key(self, client_id: int) -> str:
        key = new_key()
        self.execute("update clients set key_hash=? where id=?", (hash_key(key), client_id))
        return key

    # ---- identities ------------------------------------------------------ #
    def identity(self, client_id: int, label: str) -> dict:
        row = self.one("select * from identities where client_id=? and label=?", (client_id, label))
        if row:
            return row
        now = time.time()
        cur = self.execute("insert into identities(client_id, label, created_at) values (?,?,?)",
                           (client_id, label, now))
        return self.one("select * from identities where id=?", (cur.lastrowid,))

    # ---- stats ----------------------------------------------------------- #
    def record(self, *, client_id: int, identity_id: int, remote_ip: str, user_agent: str, event: str,
               real_up: int, rep_up: int, real_down: int, rep_down: int, status: int,
               d_real_up: int, d_fake_up: int, d_hidden_down: int) -> None:
        now = time.time()
        hour = int(now // 3600)
        ok = 200 <= status < 300
        self.execute(
            "insert into recent(at, client_id, identity_id, remote_ip, user_agent, event, real_up, rep_up,"
            " real_down, rep_down, status) values (?,?,?,?,?,?,?,?,?,?,?)",
            (now, client_id, identity_id, remote_ip, user_agent[:80], event, real_up, rep_up,
             real_down, rep_down, status))
        self.execute(
            "insert into hourly(client_id, identity_id, hour, announces, errors, real_up, fake_up, hidden_down)"
            " values (?,?,?,?,?,?,?,?)"
            " on conflict(client_id, identity_id, hour) do update set announces=announces+1,"
            " errors=errors+excluded.errors, real_up=real_up+excluded.real_up,"
            " fake_up=fake_up+excluded.fake_up, hidden_down=hidden_down+excluded.hidden_down",
            (client_id, identity_id, hour, 1, 0 if ok else 1, d_real_up, d_fake_up, d_hidden_down))
        self.execute("update clients set last_seen=? where id=?", (now, client_id))
        self.execute("update identities set last_seen=? where id=?", (now, identity_id))

    def prune(self) -> None:
        keep = self.settings()["retention_days"]
        self.execute("delete from recent where at < ?", (time.time() - keep * 86400,))
        self.execute("delete from hourly where hour < ?", (int(time.time() // 3600) - HOURLY_KEEP_DAYS * 24,))
