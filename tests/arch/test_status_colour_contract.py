"""Architecture: style.css owns the palettes, and each run-state ink stays readable.

A mermaid `style` line carries a literal hex, so the graph cannot reference a custom
property; these rules read the properties back out of style.css and compare. They also
measure every `--state-*-ink` against the `-bg` tint it is printed on.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.web.diagrams import REVIEW_STROKE, _STATUS_STROKE

_STYLESHEET = Path(__file__).resolve().parents[2] / "app" / "static" / "style.css"
_DECLARATION = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;")

# The three roles a run state declares: `--state-<name>` is the stroke/border/fill,
# `--state-<name>-bg` the tint behind it, `--state-<name>-ink` text on that tint.
_STATE_PREFIX = "state-"
_TINT_SUFFIX = "-bg"
_INK_SUFFIX = "-ink"
# WCAG 2.1 AA for body text. Run-state chips are 11px, so this is the floor, not a goal.
_MIN_CONTRAST = 4.5

# REVIEW_STROKE belief → the style.css property carrying that belief's stroke.
_BELIEF_PROPERTY = {
    "approved": "belief-approved-bd",
    "unreviewed": "belief-unreviewed-bd",
    "rejected": "belief-rejected-bd",
    "edited_stale": "belief-stale-bd",
}


def read_declared_colours() -> dict[str, str]:
    """Every `--name: #hex;` custom property in style.css, keyed without the dashes."""
    declared = {
        name: value.lower()
        for name, value in _DECLARATION.findall(_STYLESHEET.read_text(encoding="utf-8"))
    }
    if not declared:
        raise ValueError(
            f"no `--name: #hex;` custom properties found in {_STYLESHEET} — the "
            "parser is broken or the palette moved, and these rules are silently vacuous"
        )
    return declared


def find_state_accent_colours() -> dict[str, str]:
    """The `--state-*` accents: every state property that is neither a tint nor an ink."""
    return {
        name: value
        for name, value in read_declared_colours().items()
        if name.startswith(_STATE_PREFIX)
        and not name.endswith((_TINT_SUFFIX, _INK_SUFFIX))
    }


def find_state_ink_and_tint(declared: dict[str, str]) -> dict[str, tuple[str, str | None]]:
    """State name → (its ink, the tint that ink prints on, or None when undeclared)."""
    return {
        name[len(_STATE_PREFIX):-len(_INK_SUFFIX)]: (
            value, declared.get(name[: -len(_INK_SUFFIX)] + _TINT_SUFFIX)
        )
        for name, value in declared.items()
        if name.startswith(_STATE_PREFIX) and name.endswith(_INK_SUFFIX)
    }


def measure_contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.1 relative-luminance contrast of two `#rgb`/`#rrggbb` colours, 1.0–21.0."""
    lighter, darker = sorted(
        (read_relative_luminance(foreground), read_relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def read_relative_luminance(colour: str) -> float:
    digits = colour.lstrip("#")
    if len(digits) == 3:
        digits = "".join(d * 2 for d in digits)
    if len(digits) != 6:
        raise ValueError(f"{colour!r} is not a #rgb or #rrggbb colour — cannot measure it")
    channels = [int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def test_every_run_status_stroke_is_a_declared_state_colour() -> None:
    accents = find_state_accent_colours()
    assert accents, "style.css declares no --state-* accent properties"
    stray = {
        str(status): colour
        for status, (colour, _width) in _STATUS_STROKE.items()
        if colour.lower() not in set(accents.values())
    }
    assert not stray, (
        f"_STATUS_STROKE (app/web/diagrams.py) paints {stray} in colours no --state-* "
        f"property declares. Declared: {accents}. A run state the stylesheet does not "
        "know is a colour the pill, the badge and the node cannot agree on."
    )


def test_every_review_stroke_matches_its_belief_property() -> None:
    declared = read_declared_colours()
    mismatched = {
        belief: (colour, _BELIEF_PROPERTY[belief], declared.get(_BELIEF_PROPERTY[belief]))
        for belief, (colour, _width) in REVIEW_STROKE.items()
        if colour.lower() != declared.get(_BELIEF_PROPERTY[belief])
    }
    assert not mismatched, (
        "REVIEW_STROKE (app/web/diagrams.py) has drifted from the --belief-* palette in "
        f"style.css — belief: (python, property, css) {mismatched}. The legend chip and "
        "the workflow node would show different colours for the same belief."
    )


def test_every_state_ink_is_readable_on_its_tint() -> None:
    pairs = find_state_ink_and_tint(read_declared_colours())
    assert pairs, "style.css declares no --state-*-ink properties"
    illegible = {
        state: (ink, tint, round(measure_contrast_ratio(ink, tint), 2))
        for state, (ink, tint) in pairs.items()
        if tint is not None and measure_contrast_ratio(ink, tint) < _MIN_CONTRAST
    }
    assert not illegible, (
        f"--state-*-ink below WCAG AA {_MIN_CONTRAST}:1 on its own --state-*-bg tint — "
        f"state: (ink, tint, ratio) {illegible}. Darken the ink; do not darken the tint, "
        "which the base colour is also drawn against."
    )


def test_every_state_ink_has_a_tint_to_be_read_on() -> None:
    orphaned = sorted(
        state for state, (_ink, tint) in find_state_ink_and_tint(read_declared_colours()).items()
        if tint is None
    )
    assert not orphaned, (
        f"{orphaned} declare a --state-<name>-ink with no --state-<name>-bg, so the rule "
        "above silently measures nothing for them. Declare the tint or drop the ink."
    )


def test_every_belief_in_review_stroke_is_named_in_the_property_map() -> None:
    unnamed = sorted(set(REVIEW_STROKE) - set(_BELIEF_PROPERTY))
    assert not unnamed, (
        f"{unnamed} are in REVIEW_STROKE but not in _BELIEF_PROPERTY, so this file "
        "checks nothing for them — add the style.css property each one owes."
    )
