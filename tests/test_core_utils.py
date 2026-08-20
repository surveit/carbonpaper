"""app.core.utils — small dependency-free helpers. build_word_triplet_id's
own contract: three hyphen-joined parts, and a seeded generator avoids ids
already in `taken`."""
from __future__ import annotations

import random

import pytest

from app.core.utils import abbreviate_count, build_word_triplet_id


def test_generates_three_hyphen_joined_parts() -> None:
    candidate = build_word_triplet_id(set(), rng=random.Random(7))
    parts = candidate.split("-")
    assert len(parts) == 3
    assert all(parts)


def test_avoids_taken_ids_under_a_seeded_generator() -> None:
    rng = random.Random(7)
    first = build_word_triplet_id(set(), rng=rng)
    # Re-seed identically so the second call would reproduce `first`'s draws
    # were `taken` not honored; passing it as taken forces a different pick.
    rng2 = random.Random(7)
    second = build_word_triplet_id({first}, rng=rng2)
    assert second != first


# ── abbreviate_count ─────────────────────────────────────────────────────────
# The review guide's rail is 360px wide, so a measured row count is shown rounded
# there. This is the ONLY lossy rendering of a measured number in the interface,
# which is why every boundary of the rule is pinned here rather than left to the
# template: the exact count still has to be recoverable from what the rail shows.

@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0"),                     # measured and empty — a 0, never an "unknown"
        (1, "1"),
        (999, "999"),                 # last exact count
        (1_000, "1k"),                # first abbreviated one, and a bare `.0` dropped
        (45_043, "45k"),              # rounds to 45.0 → the `.0` goes
        (45_061, "45.1k"),
        (45_603, "45.6k"),
        (999_999, "1000k"),           # still under a million, so still `k`
        (1_000_000, "1m"),
        (54_423_352, "54.4m"),
    ],
)
def test_abbreviates_a_count_at_each_boundary_of_the_rule(count: int, expected: str) -> None:
    assert abbreviate_count(count) == expected


def test_a_negative_is_not_a_count_and_is_refused() -> None:
    with pytest.raises(ValueError):
        abbreviate_count(-1)
