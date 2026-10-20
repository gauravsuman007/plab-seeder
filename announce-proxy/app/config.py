"""Environment-driven configuration. Everything runtime-tunable lives in the
UI (the `settings` table); only ports, paths and the UI password come from env."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
ANNOUNCE_PORT = int(os.environ.get("ANNOUNCE_PORT", "8101"))   # clients / reverse proxy hit this
UI_PORT = int(os.environ.get("UI_PORT", "8102"))               # management UI + admin API; keep private
UI_PASSWORD = os.environ.get("UI_PASSWORD", "")                # optional HTTP Basic (any username)
