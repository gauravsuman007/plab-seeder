"""Which releases to seed.

The goal is credited upload per byte downloaded, because every byte downloaded
is spent from a fixed budget (the 2 GB guard). A release's leechers each want the
whole thing, and the seeders already there share that demand, so the expected
upload for every byte we fetch is roughly leechers / (seeders + 1) -- whatever
the size. Size then decides only whether it fits what is left of the budget,
so the selection is a greedy fill by that ratio.

Two refinements:
- grabs (completed downloads) mark a release people keep coming back to, so the
  demand outlasts the current leechers;
- a single existing seeder means we are likely to become the only one when it
  leaves, and upload as the only seeder is credited twice (the "bonus").
"""

import math
from dataclasses import asdict, dataclass

from .limits import Guard
from .parsing import Release


@dataclass
class Rules:
    min_leechers: int = 2
    max_seeders: int = 10
    min_size_mb: int = 20
    max_size_mb: int = 4096
    exclude_forums: tuple[int, ...] = ()


@dataclass
class Candidate:
    release: Release
    score: float
    cost: int                            # bytes the guard charges for it
    fits: bool
    blocked: str | None                  # why it can't be added, if it can't

    def to_dict(self) -> dict:
        d = asdict(self.release)
        d.update(score=round(self.score, 3), cost=self.cost, fits=self.fits, blocked=self.blocked)
        return d


def score(r: Release) -> float:
    if r.seeders < 1 or r.leechers < 1:
        return 0.0          # no source to fetch from, or nobody to upload to
    demand = r.leechers / (r.seeders + 1)
    staying_power = 1 + 0.15 * math.log10(1 + r.grabs)
    bonus_chance = 1.3 if r.seeders == 1 else 1.0
    return demand * staying_power * bonus_chance


def eligible(r: Release, rules: Rules) -> str | None:
    """Why a release is not a candidate at all, or None."""
    if not r.downloadable:
        return "no .torrent link"
    if r.seeders < 1:
        return "no seeders"
    if r.leechers < rules.min_leechers:
        return f"fewer than {rules.min_leechers} leechers"
    if r.seeders > rules.max_seeders:
        return f"more than {rules.max_seeders} seeders"
    if r.size < rules.min_size_mb * 1024**2:
        return "too small"
    if r.size > rules.max_size_mb * 1024**2:
        return "too large"
    if r.forum_id is not None and r.forum_id in rules.exclude_forums:
        return "forum excluded"
    return None


def rank(releases: list[Release], rules: Rules, guard: Guard, known: set[int]) -> list[Candidate]:
    """Every release scored, best first, each marked with whether it fits now."""
    out = []
    seen = set()
    for r in releases:
        if r.topic_id in known or r.topic_id in seen:
            continue
        seen.add(r.topic_id)
        why = eligible(r, rules)
        # Scored even when the guard blocks it, so the UI can show what would
        # be next once there is room; ineligible releases score nothing.
        s = score(r) if why is None else 0.0
        cost = guard.cost(r.size)
        fits = why is None and guard.fits(r.size)
        if why is None and not fits:
            why = "would cross the download guard"
        out.append(Candidate(r, s, cost, fits, why))
    out.sort(key=lambda c: (c.blocked is not None, -c.score))
    return out


def pick(candidates: list[Candidate], headroom: int, slots: int, *, size_bias: float = 0.0) -> list[Candidate]:
    """Greedy fill: best score first, skipping any that no longer fit.

    size_bias > 0 re-sorts the eligible pool toward smaller releases first,
    for when the ratio is poor and auto-seed should ease back in with small
    files rather than spend a thin byte budget on one big one. 0 (default)
    keeps the plain best-score-first order.
    """
    eligible = [c for c in candidates if c.blocked is None and c.score > 0]
    if size_bias:
        # 64 MB keeps the exponent well-behaved for typical release sizes
        # instead of blowing up on anything under a megabyte.
        eligible = sorted(eligible, key=lambda c: -(c.score / (1 + c.release.size / (64 * 1024**2)) ** size_bias))
    chosen = []
    left = headroom
    for c in eligible:
        if len(chosen) >= slots:
            break
        if c.cost <= left:
            chosen.append(c)
            left -= c.cost
    return chosen
