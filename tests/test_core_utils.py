"""app.core.utils — small dependency-free helpers. generate_word_triplet_id's
own contract: three hyphen-joined parts, and a seeded generator avoids ids
already in `taken`."""
from __future__ import annotations

import random

from app.core.utils import generate_word_triplet_id


def test_generates_three_hyphen_joined_parts() -> None:
    candidate = generate_word_triplet_id(set(), rng=random.Random(7))
    parts = candidate.split("-")
    assert len(parts) == 3
    assert all(parts)


def test_avoids_taken_ids_under_a_seeded_generator() -> None:
    rng = random.Random(7)
    first = generate_word_triplet_id(set(), rng=rng)
    # Re-seed identically so the second call would reproduce `first`'s draws
    # were `taken` not honored; passing it as taken forces a different pick.
    rng2 = random.Random(7)
    second = generate_word_triplet_id({first}, rng=rng2)
    assert second != first
