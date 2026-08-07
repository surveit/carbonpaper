"""Architecture: a control carrying a hue keeps that hue on hover.

`.btn:hover` sets the paper `--hover`, and a variant like `.btn.approve` inherits it —
so a green button turned beige under the cursor, which reads as the colour draining
out rather than as the button responding. A variant with a tint owes its own hover.
"""
from __future__ import annotations

import re
from pathlib import Path

_STYLESHEET = Path(__file__).resolve().parents[2] / "app" / "static" / "style.css"
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
# `background: var(--<hue>-bg)` is what makes a control coloured rather than paper.
_TINTED = re.compile(r"background(?:-color)?\s*:\s*var\(--([a-z]+)-bg\)")


def read_rules() -> list[tuple[str, str]]:
    """(selector list, declaration block) for every rule in style.css."""
    rules = [
        (sel.strip(), body)
        for sel, body in _RULE.findall(_COMMENT.sub("", _STYLESHEET.read_text(encoding="utf-8")))
        if not sel.strip().startswith("@")
    ]
    if not rules:
        raise ValueError(f"no rules parsed out of {_STYLESHEET} — this file checks nothing")
    return rules


def find_tinted_variants() -> dict[str, str]:
    """Variant class (`.btn.approve`) → the hue its background carries."""
    tinted = {}
    for selector, body in read_rules():
        hue = _TINTED.search(body)
        if hue is None:
            continue
        for one in selector.split(","):
            one = one.strip()
            if one.count(".") >= 2 and ":" not in one and " " not in one:
                tinted[one] = hue.group(1)
    return tinted


def find_selectors_with_a_hover() -> set[str]:
    return {
        one.strip().removesuffix(":hover")
        for selector, _ in read_rules()
        for one in selector.split(",")
        if one.strip().endswith(":hover")
    }


def test_a_tinted_control_does_not_inherit_the_paper_hover() -> None:
    hovered = find_selectors_with_a_hover()
    stranded = {
        variant: hue
        for variant, hue in find_tinted_variants().items()
        # only a variant whose BASE element has a hover can inherit the wrong one
        if variant.rsplit(".", 1)[0] in hovered and variant not in hovered
    }
    assert not stranded, (
        f"{stranded} carry a hue tint but declare no :hover of their own, so they "
        "inherit their base element's, which is the paper --hover. Under the cursor "
        "the colour that says what the control does drains away. Give each one "
        "`color-mix(in srgb, var(--<hue>-bd) 16%, var(--<hue>-bg))` — one step toward "
        "its own border, staying inside its hue."
    )


def test_the_scan_finds_the_tinted_controls_it_is_meant_to_guard() -> None:
    """A changed selector style would make the rule above silently vacuous."""
    tinted = find_tinted_variants()
    assert len(tinted) >= 4, f"only found {tinted} — the parser has drifted from the CSS"
    assert ".btn.approve" in tinted, f"the approve button is not being seen: {sorted(tinted)}"
