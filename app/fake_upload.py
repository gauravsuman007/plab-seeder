"""Fake upload/download reporting, shared by the full app's announce proxy
(app/main.py) and the qBittorrent-free standalone mode (app/standalone.py).

Two independent fakes, both keyed off wall-clock time so the numbers are
stable across restarts and look like organic traffic rather than a clock:

- `bootstrap_downloaded_bytes`: a brand new account's very first tracked
  torrent gets its downloaded total ramped up toward ~1 GiB over its first
  hour, instead of the usual downloaded=0, so a fresh profile shows a
  believable small download rather than "uploads with no download history".
- `fake_upload_delta_bytes`: a randomised per-announce upload delta that
  averages to a target MB/hour over a day, with wide, non-repeating
  variance (see its own docstring for the design goals).
"""

import math
import random
import time

BOOTSTRAP_WINDOW_S = 3600
BOOTSTRAP_TARGET_BYTES = 1_000_000_000


def fake_upload_delta_bytes(token: str, mb_per_hour: int, elapsed_s: float = 1800) -> int:
    """Return a randomised per-announce upload delta that averages to
    mb_per_hour over a day, with wide, non-repeating variance.

    Design goals:
    - Daily total stays within ±10 % of target (mb_per_hour × 24 hours).
    - No fixed period: combines multiple incommensurate sine waves so the
      pattern never visibly repeats on a human timescale.
    - Extra salt from the announce token keeps torrents out of phase with
      each other, avoiding a suspicious all-torrents-spike-at-once pattern.

    `elapsed_s` is the time this delta covers (default: qBittorrent's 30
    minute announce interval); the amount scales with it, so the daily total
    tracks mb_per_hour whatever the real announce cadence is.
    """
    target_bytes_per_hour = mb_per_hour * 1024 * 1024
    tok_salt = int.from_bytes(token.encode()[:4].ljust(4, b"\x00"), "big")

    now = time.time()
    # Three incommensurate periods (in seconds): ~47 min, ~1.7 h, ~4.1 h
    phases = [
        math.sin(2 * math.pi * now / 2820 + tok_salt * 0.37),
        math.sin(2 * math.pi * now / 6300 + tok_salt * 0.71),
        math.sin(2 * math.pi * now / 14700 + tok_salt * 1.13),
    ]
    wave = (phases[0] * 0.5 + phases[1] * 0.3 + phases[2] * 0.2)

    multiplier = 1.0 + wave * 0.95
    multiplier = max(0.1, min(3.0, multiplier))
    noise = random.uniform(0.70, 1.30)
    multiplier *= noise

    base = target_bytes_per_hour * (elapsed_s / 3600.0)
    delta = int(base * multiplier)
    return max(0, delta)


def fake_download_delta_bytes(token: str, mb_per_hour: int, elapsed_s: float = 1800) -> int:
    """Return a randomised per-announce download delta, same variance model as
    fake_upload_delta_bytes but phase-shifted so upload and download deltas
    don't move in lockstep."""
    return fake_upload_delta_bytes(token + "_dl", mb_per_hour, elapsed_s)


def bootstrap_downloaded_bytes(target: int, first_download_at: float) -> int:
    """Interpolate toward a one-time target, frozen after the window.

    `target` is the (already randomised, ±10%) total this account's
    bootstrap torrent should ramp toward -- callers persist it once
    (see `bootstrap_target_bytes`) so repeated calls agree across restarts.
    """
    elapsed = time.time() - first_download_at
    if elapsed >= BOOTSTRAP_WINDOW_S:
        return target
    frac = max(0.0, min(1.0, elapsed / BOOTSTRAP_WINDOW_S))
    # Ease toward the target with a little jitter so it isn't a perfectly
    # straight ramp (announce-to-announce noise, same spirit as the fake
    # upload curve above), but always monotonically non-decreasing in
    # expectation and capped at the target.
    jitter = random.uniform(0.95, 1.05)
    return max(0, min(target, int(target * frac * jitter)))


def bootstrap_target_bytes() -> int:
    """A one-time ±10% target around BOOTSTRAP_TARGET_BYTES; persist the result."""
    return int(BOOTSTRAP_TARGET_BYTES * random.uniform(0.9, 1.1))
