"""app.core.utils — the shared, dependency-free helpers."""
from __future__ import annotations

import random

from app.core.utils import generate_word_triplet_id


def test_word_triplet_id_has_three_parts() -> None:
    assert len(generate_word_triplet_id(set()).split("-")) == 3


def test_word_triplet_id_avoids_taken() -> None:
    first = generate_word_triplet_id(set(), rng=random.Random(7))
    second = generate_word_triplet_id({first}, rng=random.Random(7))
    assert first != second
