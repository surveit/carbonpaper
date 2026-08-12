"""Architecture: every severity renders its own shape, the way every stage type has a glyph.

The macro returns "" for a severity it has no branch for, so a new
UserFacingErrorSeverity member renders as a word and a colour alone — silently.
"""
from __future__ import annotations

from typing import Any

from app.models.severity import UserFacingErrorSeverity
from app.web.config import templates


def render_icon(severity: str) -> str:
    # The macro is an attribute the TEMPLATE defines, so no stub can know it.
    module: Any = templates.env.get_template("_severity_icon.html").module
    return str(module.severity_icon(severity))


def test_every_severity_renders_an_icon() -> None:
    bare = [s.value for s in UserFacingErrorSeverity if "<svg" not in render_icon(s.value)]
    assert not bare, (
        f"{bare} render no icon — _severity_icon.html has no branch for them, so they reach "
        "the reader as a word and a colour alone, which is what the shape was added to fix."
    )


def test_no_two_severities_share_a_shape() -> None:
    drawn = {s.value: render_icon(s.value) for s in UserFacingErrorSeverity}
    assert len(set(drawn.values())) == len(drawn), (
        f"two of {sorted(drawn)} draw identical markup, so only their colour tells them "
        "apart — the shape has to differ or it is not a second signal."
    )
