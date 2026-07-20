"""Small dependency-free helpers shared across app layers.

Pure functions only. app.core sits at the bottom of the import graph, so nothing
here may import an app subsystem — these helpers take plain values and return
plain values."""
from __future__ import annotations

import random

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
