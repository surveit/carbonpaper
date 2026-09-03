"""Architecture: palette.css owns every colour, and each ink stays readable on its tint.

A mermaid `style` line takes a literal hex and cannot reference a custom property, so
app/web/diagrams.py repeats the palette in Python; every literal there is compared here
against the property it copies, and each `--*-ink` against the tint it prints on.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.web.diagrams import _NODE_SURFACE, _STATUS_STROKE, build_schema_table_graph
from app.web.walk_diagram import (
    WALK_ASIDE_FILL,
    WALK_ASIDE_INK,
    WALK_LIT_STROKE,
    WALK_QUIET_STROKE,
)

_PALETTE = Path(__file__).resolve().parents[2] / "app" / "static" / "palette.css"
_DECLARATION = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8}|var\(--[a-z0-9-]+\))\s*;")
_INDIRECTION = re.compile(r"var\(--([a-z0-9-]+)\)")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)

# The three roles a run state declares: `--state-<name>` is the stroke/border/fill,
# `--state-<name>-bg` the tint behind it, `--state-<name>-ink` text on that tint.
_DEFAULT_THEME_SELECTOR = ":root {"
_STATE_PREFIX = "state-"
_TINT_SUFFIX = "-bg"
_INK_SUFFIX = "-ink"
# WCAG 2.1 AA for body text. Run-state chips are 11px, so this is the floor, not a goal.
_MIN_CONTRAST = 4.5
_CLASSDEF = re.compile(r"classDef (\w+) (\S+)")

# One named schema per kind, so build_schema_table_graph emits every classDef.
_ONE_SCHEMA_PER_KIND = [
    {"name": "reference_kind", "kind": "reference"}, {"name": "input_kind", "kind": "input"},
    {"name": "computed_kind", "kind": "computed"}, {"name": "truth_kind", "kind": "ground_truth"},
]



def read_declared_colours() -> dict[str, str]:
    declared = {
        name: value.lower()
        for name, value in _DECLARATION.findall(read_default_theme_rules())
    }
    if not declared:
        raise ValueError(
            f"no `--name: <colour>;` custom properties found in {_PALETTE} — the "
            "parser is broken or the palette moved, and these rules are silently vacuous"
        )
    return {name: resolve_indirection(name, declared) for name in declared}


def read_palette_rules() -> str:
    """Raw text would match a declaration a mis-closed comment had swallowed."""
    return _COMMENT.sub("", _PALETTE.read_text(encoding="utf-8"))


def read_default_theme_rules() -> str:
    """A whole-file scan would pin these rules to whichever theme block sits last."""
    return read_theme_rules(_DEFAULT_THEME_SELECTOR)


def read_theme_rules(selector: str) -> str:
    rules = read_palette_rules()
    start = rules.index(selector) + len(selector)
    return rules[start:rules.index("}", start)]


def resolve_indirection(name: str, declared: dict[str, str]) -> str:
    seen: list[str] = []
    while (target := _INDIRECTION.fullmatch(declared[name])) is not None:
        seen.append(name)
        name = target.group(1)
        if name in seen or name not in declared:
            raise ValueError(f"--{name} is an unresolvable alias; chain so far: {seen}")
    return declared[name]


def find_ink_and_tint(declared: dict[str, str], prefix: str) -> dict[str, tuple[str, str | None]]:
    return {
        name[len(prefix):-len(_INK_SUFFIX)]: (
            value, declared.get(name[: -len(_INK_SUFFIX)] + _TINT_SUFFIX)
        )
        for name, value in declared.items()
        if name.startswith(prefix) and name.endswith(_INK_SUFFIX)
    }


def find_state_accent_colours() -> dict[str, str]:
    return {
        name: value
        for name, value in read_declared_colours().items()
        if name.startswith(_STATE_PREFIX)
        and not name.endswith((_TINT_SUFFIX, _INK_SUFFIX))
    }


def measure_contrast_ratio(foreground: str, background: str) -> float:
    """Contrast of two `#rgb`/`#rrggbb` colours, on WCAG 2.1's 1.0–21.0 scale."""
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
    assert accents, "palette.css declares no --state-* accent properties"
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


def test_every_state_ink_is_readable_on_its_tint() -> None:
    pairs = find_ink_and_tint(read_declared_colours(), _STATE_PREFIX)
    assert pairs, "palette.css declares no --state-*-ink properties"
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
        state for state, (_ink, tint) in find_ink_and_tint(read_declared_colours(), _STATE_PREFIX).items()
        if tint is None
    )
    assert not orphaned, (
        f"{orphaned} declare a --state-<name>-ink with no --state-<name>-bg, so the rule "
        "above silently measures nothing for them. Declare the tint or drop the ink."
    )


def test_every_hue_ink_is_readable_on_its_tint() -> None:
    pairs = find_ink_and_tint(read_declared_colours(), "")
    assert pairs, "palette.css declares no --*-ink properties"
    illegible = {
        hue: (ink, tint, round(measure_contrast_ratio(ink, tint), 2))
        for hue, (ink, tint) in pairs.items()
        if tint is not None and measure_contrast_ratio(ink, tint) < _MIN_CONTRAST
    }
    assert not illegible, (
        f"--*-ink below WCAG AA {_MIN_CONTRAST}:1 on its own --*-bg tint — "
        f"hue: (ink, tint, ratio) {illegible}. Darken the ink; do not lighten the tint, "
        "which that hue's -bd is also drawn against."
    )


def test_no_schema_kind_classdef_carries_a_colour() -> None:
    emitted = dict(_CLASSDEF.findall(build_schema_table_graph(_ONE_SCHEMA_PER_KIND)))
    assert emitted, "build_schema_table_graph emitted no classDefs — this rule is vacuous"
    coloured = {k: v for k, v in emitted.items() if v != _NODE_SURFACE}
    assert not coloured, (
        f"{coloured} paint a schema kind in a colour of its own. Colour on these pages "
        "answers what happened and whether we trust it; a kind answers neither, and a "
        f"tinted node puts the two axes in one vocabulary. Expected {_NODE_SURFACE}."
    )


def test_the_default_node_surface_is_the_sheet() -> None:
    declared = read_declared_colours()
    expected = (f"fill:{declared['bg']},stroke:{declared['border']},"
                f"color:{declared['fg']}")
    assert _NODE_SURFACE == expected, (
        f"_NODE_SURFACE (app/web/diagrams.py) is {_NODE_SURFACE!r}, but palette.css's "
        f"--bg / --border / --fg say {expected!r}. An unstated stage type would sit on a "
        "different paper from the page around it."
    )


def test_no_palette_comment_closes_early() -> None:
    leftover = read_palette_rules()
    assert "*/" not in leftover, (
        "a comment in palette.css closes before its author meant it to — prose "
        "containing `*/` (writing L*/C*, say) ends the comment there, and the rest "
        "of it becomes junk that eats the declaration below. Every token still "
        "matches by regex, so only this rule notices."
    )


def test_every_token_a_var_points_at_is_declared() -> None:
    declared = read_declared_colours()
    dangling = sorted(
        {name for name in _INDIRECTION.findall(read_palette_rules())} - set(declared)
    )
    assert not dangling, (
        f"{dangling} are referenced by a var() in palette.css but declared nowhere in "
        "it, so every property spending them falls back to currentColor and silently "
        "paints the wrong thing."
    )


def test_every_walk_state_colour_is_the_palette_property_it_copies() -> None:
    declared = read_declared_colours()
    expected = {
        "WALK_LIT_STROKE": declared["accent"],
        "WALK_QUIET_STROKE": declared["border-strong"],
        "WALK_ASIDE_FILL": declared["sunk-deep"],
        "WALK_ASIDE_INK": declared["muted-dim"],
    }
    written = {
        "WALK_LIT_STROKE": WALK_LIT_STROKE.lower(),
        "WALK_QUIET_STROKE": WALK_QUIET_STROKE.lower(),
        "WALK_ASIDE_FILL": WALK_ASIDE_FILL.lower(),
        "WALK_ASIDE_INK": WALK_ASIDE_INK.lower(),
    }
    assert written == expected, (
        f"app/web/walk_diagram.py writes {written}, palette.css says {expected}. The "
        "column walk's three node states spend the sheet's own greys and its accent — "
        "borrowing a --state-* hue would say a stage off the walk had been cancelled."
    )
