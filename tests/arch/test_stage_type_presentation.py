"""Architecture: every stage type the models define is drawn by the diagram maps.

An unmapped type falls through `TYPE_CLASS.get(stype, 'custom')` to the `custom`
class — the red fill that means *error* on every other surface — so a healthy
workflow paints broken nodes. `union` and `filter_rows` shipped that way.
"""
from __future__ import annotations

import re

from app.models import StageType
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from arch._helpers import read_stylesheets

# The class `TYPE_CLASS.get(stype, ...)` falls back to, so it must be styled too.
_FALLBACK_CLASS = "custom"


def find_types_missing_from(presentation_map: dict[str, str]) -> list[str]:
    return sorted(t.value for t in StageType if t.value not in presentation_map)


def collect_emittable_classes() -> set[str]:
    return set(TYPE_CLASS.values()) | {_FALLBACK_CLASS}


def test_every_stage_type_has_a_node_class() -> None:
    missing = find_types_missing_from(TYPE_CLASS)
    assert not missing, (
        f"{missing} have no TYPE_CLASS entry (app/web/diagrams.py), so their nodes and "
        f"type tags render in the `{_FALLBACK_CLASS}` red palette that means error elsewhere."
    )


def test_every_stage_type_has_a_glyph() -> None:
    missing = find_types_missing_from(TYPE_GLYPH)
    assert not missing, (
        f"{missing} have no TYPE_GLYPH entry (app/web/diagrams.py), so their nodes and "
        "type tags render with a blank glyph slot."
    )


def test_every_node_class_has_a_mermaid_classdef() -> None:
    declared = set(re.findall(r"classDef (\w+) ", build_mermaid_graph([], "demo")))
    assert declared, "build_mermaid_graph emitted no classDef lines — has the palette moved?"
    missing = sorted(collect_emittable_classes() - declared)
    assert not missing, (
        f"{missing} are node classes TYPE_CLASS can emit, but build_mermaid_graph declares "
        "no classDef for them, so those nodes render unstyled."
    )


def test_every_node_class_has_a_badge_rule() -> None:
    styled = set(re.findall(r"\.badge\.(\w+)", read_stylesheets()))
    assert styled, "no `.badge.<class>` rules found in app/static/*.css — this rule is vacuous"
    missing = sorted(collect_emittable_classes() - styled)
    assert not missing, (
        f"{missing} are node classes TYPE_CLASS can emit, but no app/static/*.css has a "
        "`.badge.<class>` rule, so the type tag beside the node is unstyled."
    )
