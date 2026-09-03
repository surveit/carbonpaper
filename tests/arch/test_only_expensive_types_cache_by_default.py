"""Architecture: `cache` defaults to true on llm_transform alone."""
from __future__ import annotations

from typing import get_args

from app.models.stage import Stage

CACHE_BY_DEFAULT = {"llm_transform"}


def collect_cache_defaults() -> dict[str, bool]:
    members = get_args(get_args(Stage)[0])
    assert members, "Stage is no longer an Annotated union — this test cannot read it"
    return {
        member.model_fields["type"].annotation.__args__[0].value:
            bool(member.model_fields["cache"].default)
        for member in members
    }


def test_only_the_expensive_types_cache_without_being_asked() -> None:
    caching = {name for name, on in collect_cache_defaults().items() if on}
    assert caching == CACHE_BY_DEFAULT, (
        f"{sorted(caching)} cache by default. Only {sorted(CACHE_BY_DEFAULT)} may: every "
        "other type recomputes for less than the fingerprint of its own input, and an "
        "author who knows better sets `cache: true` on the one stage that needs it."
    )
