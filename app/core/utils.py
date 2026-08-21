"""Small dependency-free helpers shared across app layers.

Pure functions only. app.core sits at the bottom of the import graph, so nothing
here may import an app subsystem — these helpers take plain values and return
plain values."""
from __future__ import annotations

import hashlib
import random

from pydantic import ValidationError

from app.core.ids import ID

_ADJECTIVES = ("amber", "brisk", "calm", "dusky", "eager", "fresh", "glad", "keen",
               "lucid", "mellow", "noble", "plain", "quiet", "rapid", "solid", "tidy")
_ANIMALS = ("badger", "crane", "finch", "gecko", "heron", "ibis", "lynx", "marmot",
            "mole", "newt", "otter", "owl", "pika", "raven", "seal", "toad")
_THINGS = ("brook", "cove", "delta", "dune", "fern", "glen", "knoll", "lamp",
           "mesa", "pond", "reef", "ridge", "shoal", "vale", "wharf", "yard")


def build_word_triplet_id(taken: set[str], rng: random.Random | None = None) -> ID:
    rng = rng or random.Random()
    for _ in range(10_000):
        candidate = "-".join(
            (rng.choice(_ADJECTIVES), rng.choice(_ANIMALS), rng.choice(_THINGS))
        )
        if candidate not in taken:
            return candidate
    raise RuntimeError("Word-triplet id space exhausted")


# ── Content hashing ──────────────────────────────────────────────────────────
def compute_short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ── Count abbreviation ───────────────────────────────────────────────────────
# Largest unit first, so a count is spent on the biggest one it reaches. The unit
# follows from the count's own magnitude and nothing else — no unit is skipped to
# make a rounder label — so 999,999 reads as `1000k`, not as `1m`: it is under a
# million, and the `m` would be the interface rounding a measured number across a
# unit boundary the reader is using to judge the size.
_ABBREVIATION_UNITS = ((1_000_000, "m"), (1_000, "k"))


def abbreviate_count(n: int) -> str:
    """LOSSY (`45061` → `45.1k`) — the caller must keep the exact count reachable."""
    if n < 0:
        raise ValueError(f"Not a count: {n}")
    for unit, suffix in _ABBREVIATION_UNITS:
        if n >= unit:
            # One decimal, and a trailing `.0` is dropped rather than shown: `45k`
            # says the same as `45.0k` and reads as a number instead of a reading.
            return f"{round(n / unit, 1):.1f}".removesuffix(".0") + suffix
    return str(n)


# ── Error formatting ─────────────────────────────────────────────────────────
def format_errors(err: ValidationError) -> list[str]:
    out: list[str] = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "stages")
        msg = e.get("msg", "invalid")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out
