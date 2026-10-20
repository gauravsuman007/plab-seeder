"""The numbers the proxy reports. Same model as the seeder's app/fake_upload.py
(kept as a copy so this image has no dependency on the seeder's).

- `upload_delta_bytes`: a randomised amount covering `elapsed_s` seconds that
  averages to `mb_per_hour` over a day. Three incommensurate sine waves plus
  uniform noise, salted by the token, so there's no repeating pattern.
- `bootstrap_*`: a brand-new account's very first torrent ramps a believable
  ~1 GB downloaded total over its first hour instead of reporting 0.
"""

import math
import random
import time

BOOTSTRAP_WINDOW_S = 3600
BOOTSTRAP_TARGET_BYTES = 1_000_000_000


def upload_delta_bytes(token: str, mb_per_hour: int, elapsed_s: float = 1800) -> int:
    salt = int.from_bytes(token.encode()[:4].ljust(4, b"\x00"), "big")
    now = time.time()
    wave = (math.sin(2 * math.pi * now / 2820 + salt * 0.37) * 0.5
            + math.sin(2 * math.pi * now / 6300 + salt * 0.71) * 0.3
            + math.sin(2 * math.pi * now / 14700 + salt * 1.13) * 0.2)
    multiplier = max(0.1, min(3.0, 1.0 + wave * 0.95)) * random.uniform(0.70, 1.30)
    return max(0, int(mb_per_hour * 1024 * 1024 * (elapsed_s / 3600.0) * multiplier))


def bootstrap_target() -> int:
    return int(BOOTSTRAP_TARGET_BYTES * random.uniform(0.9, 1.1))


def bootstrap_downloaded(target: int, first_download_at: float) -> int:
    elapsed = time.time() - first_download_at
    if elapsed >= BOOTSTRAP_WINDOW_S:
        return target
    frac = max(0.0, min(1.0, elapsed / BOOTSTRAP_WINDOW_S))
    return max(0, min(target, int(target * frac * random.uniform(0.95, 1.05))))
