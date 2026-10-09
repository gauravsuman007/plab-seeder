"""PornoLab's account rules, and the guard that keeps us inside them.

From the site's rating FAQ (section 5), the number of .torrent files an account
may fetch per day:

    newbie (downloaded under 2 GB)      5, whatever the rating
    rating >= 1.0, uploaded >= 100 GB   100
    rating >= 1.0                       50
    rating 0.5 - 1.0                    50
    rating 0.3 - 0.5                    10
    rating < 0.3                        0 -- new downloads blocked, ban risk
    member of a group                   unlimited

Rating = (uploaded + uploaded on own releases + bonus) / downloaded, recomputed
once a day between 00:00 and 01:00 Moscow time. Downloading 100x what you
upload gets the account blocked.
"""

from dataclasses import dataclass

from .parsing import Profile

GB = 1024**3

# Where the newbie tier ends. The site says "2 GB"; the decimal reading is the
# smaller number, so it is the one we never cross.
NEWBIE_CEILING = 2_000_000_000


@dataclass
class Tier:
    name: str
    daily_torrents: int | None          # None = unlimited
    note: str


def tier(profile: Profile) -> Tier:
    if profile.newbie or profile.live_downloaded < NEWBIE_CEILING:
        return Tier("newbie", 5, "Under 2 GB downloaded: 5 .torrent files a day, rating not applied yet.")
    r = profile.rating or 0.0
    if r >= 1.0:
        if profile.credited_upload >= 100 * GB:
            return Tier("rating >= 1.0, 100 GB+ up", 100, "")
        return Tier("rating >= 1.0", 50, "")
    if r >= 0.5:
        return Tier("rating 0.5-1.0", 50, "")
    if r >= 0.3:
        return Tier("rating 0.3-0.5", 10, "")
    return Tier("rating < 0.3", 0, "New .torrent downloads are blocked until the rating is back above 0.3.")


@dataclass
class Guard:
    """How much more this account may download, and why."""
    ceiling: int                        # the byte count we must stay under
    live_downloaded: int                # from the profile
    pending: int                        # still to come from torrents downloading now (+overhead)
    ledger: int                         # our own count: baseline + everything we ever added (+overhead)
    committed: int                      # what the guard charges as already spent: the larger view
    overhead: float                     # allowance for protocol overhead / re-downloaded pieces
    headroom: int                       # bytes a new torrent may cost, after all of the above
    mode: str                           # "never-cross", "ratio" or "override"
    reason: str
    ratio: float = 1.0                  # credited upload / committed, however thin

    def cost(self, size: int) -> int:
        return int(size * (1 + self.overhead))

    def fits(self, size: int) -> bool:
        return self.cost(size) <= self.headroom


def guard(profile: Profile, pending_bytes: int, ledger_bytes: int = 0, *,
          allow_crossing: bool = False, ratio_target: float = 0.6, overhead: float = 0.10,
          override: bool = False) -> Guard:
    """The download budget.

    Two views of what is already spent, and the larger one wins:
    - the profile: its live downloaded count, plus what is still to come from
      torrents downloading now;
    - the ledger: the profile's count when the seeder first saw it, plus the
      full size of every torrent the seeder has ever added. The tracker only
      learns what qBittorrent downloaded at the next announce (up to an hour
      later), so the profile alone can lag behind the truth; the ledger can't.

    never-cross (default): committed + every new torrent (each with `overhead`
    added) must stay under NEWBIE_CEILING. Below 2 GB the 5-a-day cap never
    lifts, but the account can never fall into "rating < 0.3: blocked".

    ratio (allow_crossing): past 2 GB, a download is allowed only while the
    credited upload stays at least `ratio_target` times the new downloaded total.

    override: the owner's explicit choice to ignore this guard altogether
    (e.g. the account was already past the line from downloads made outside
    this app, so never-cross/ratio would permanently read zero headroom).
    The site's own daily .torrent quota (see tier()/budget()) still applies --
    this only removes the app's own, stricter safety margin.
    """
    live = profile.live_downloaded
    pending = int(pending_bytes * (1 + overhead))
    committed = max(live + pending, ledger_bytes)
    up = profile.live_credited_upload
    ratio = (up / committed) if committed else 1.0

    if override:
        return Guard(NEWBIE_CEILING, live, pending, ledger_bytes, committed, overhead,
                     2**62, "override", "Guard override is on: download limit ignored (ban risk)", ratio)

    if not allow_crossing:
        headroom = max(0, NEWBIE_CEILING - committed)
        reason = (f"{fmt(headroom)} left under the 2 GB line"
                  if headroom else "At the 2 GB line: no new downloads, seeding continues")
        return Guard(NEWBIE_CEILING, live, pending, ledger_bytes, committed, overhead, headroom,
                     "never-cross", reason, ratio)

    # credited_up / (committed + x) >= target  =>  x <= credited_up/target - committed
    ratio_room = int(up / ratio_target) - committed if ratio_target > 0 else 0
    newbie_room = NEWBIE_CEILING - committed
    headroom = max(0, max(ratio_room, newbie_room))
    reason = (f"{fmt(headroom)} before the rating would fall under {ratio_target}"
              if ratio_room >= newbie_room else f"{fmt(headroom)} left under the 2 GB line")
    ceiling = max(NEWBIE_CEILING, int(up / ratio_target) if ratio_target > 0 else 0)
    return Guard(ceiling, live, pending, ledger_bytes, committed, overhead, headroom, "ratio", reason, ratio)


def fmt(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"
