"""Small dependency-free helpers shared across app layers.

Pure functions only. app.core sits at the bottom of the import graph, so nothing
here may import an app subsystem — these helpers take plain values and return
plain values."""
from __future__ import annotations

import hashlib
import random
import re

from pydantic import ValidationError


# ── Identifiers ──────────────────────────────────────────────────────────────
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def is_snake_case(value: str) -> bool:
    """True if `value` is a lowercase identifier: a leading letter, then letters,
    digits or underscores. Rejects a leading digit or underscore."""
    return _SNAKE_RE.match(value) is not None


_ADJECTIVES = ("amber", "brisk", "calm", "dusky", "eager", "fresh", "glad", "keen",
               "lucid", "mellow", "noble", "plain", "quiet", "rapid", "solid", "tidy")
_ANIMALS = ("badger", "crane", "finch", "gecko", "heron", "ibis", "lynx", "marmot",
            "mole", "newt", "otter", "owl", "pika", "raven", "seal", "toad")
_THINGS = ("brook", "cove", "delta", "dune", "fern", "glen", "knoll", "lamp",
           "mesa", "pond", "reef", "ridge", "shoal", "vale", "wharf", "yard")


def generate_word_triplet_id(taken: set[str], rng: random.Random | None = None) -> str:
    """A word-triplet id (e.g. brisk-otter-lamp) not in `taken` — short enough to
    retype reliably and unmistakable for a timestamp id. 4096 combinations dwarf
    the handful of live ids any one caller holds; fails loudly if the space is
    somehow exhausted rather than looping forever."""
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
    """The first 16 hex characters of the SHA-1 digest of `text`
    (UTF-8 encoded): `sha1(text.encode("utf-8")).hexdigest()[:16]`."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ── Error formatting ─────────────────────────────────────────────────────────
def format_errors(err: ValidationError) -> list[str]:
    """Pydantic errors → human-readable issue strings."""
    out: list[str] = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "stages")
        msg = e.get("msg", "invalid")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out
