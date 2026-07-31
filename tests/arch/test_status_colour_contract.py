"""Architecture: the diagram's stroke palettes are the ones style.css declares.

A mermaid `style` line carries a literal hex, so the graph cannot reference a CSS
custom property. This reads the properties back out of style.css and compares, so
the two copies fail loudly instead of drifting behind a comment promising they match.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.web.diagrams import REVIEW_STROKE, _STATUS_STROKE

_STYLESHEET = Path(__file__).resolve().parents[2] / "app" / "static" / "style.css"
_DECLARATION = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;")

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
    """The `--state-*` accents (fg/stroke), i.e. every state property bar its `-bg` tint."""
    return {
        name: value
        for name, value in read_declared_colours().items()
        if name.startswith("state-") and not name.endswith("-bg")
    }


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


def test_every_belief_in_review_stroke_is_named_in_the_property_map() -> None:
    unnamed = sorted(set(REVIEW_STROKE) - set(_BELIEF_PROPERTY))
    assert not unnamed, (
        f"{unnamed} are in REVIEW_STROKE but not in _BELIEF_PROPERTY, so this file "
        "checks nothing for them — add the style.css property each one owes."
    )
