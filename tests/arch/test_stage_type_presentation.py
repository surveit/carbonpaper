"""Architecture: every stage type the models define is drawn by the diagram maps.

An unmapped type falls through `TYPE_CLASS.get(stype, 'custom')` to the `custom`
class — the red fill that means *error* on every other surface — so a healthy
workflow paints broken nodes. `union` and `filter_rows` shipped that way.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import get_args, get_type_hints

from app.models import StageType
from app.models.stage import Stage
from app.models.stages.stage_base import AbstractStage
from app.web.authored_code import BLOCK_COPY
from app.web.config import label_stage_type
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, TYPE_LABEL, build_mermaid_graph
from arch._helpers import parse_module, read_stylesheets
from arch.scope import find_source_files_under

# The class `TYPE_CLASS.get(stype, ...)` falls back to, so it must be styled too.
_FALLBACK_CLASS = "custom"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEB_ROOT = _REPO_ROOT / "app" / "web"
_TEMPLATE_ROOT = _REPO_ROOT / "app" / "templates"

# The only routes a stage type may take to the screen: the three presentation maps
# and the Jinja filter over TYPE_LABEL, under both their Python and template names.
# Reaching output any other way puts the slug in front of the reader.
_PRESENTATION_ROUTES = frozenset(
    {
        "TYPE_LABEL",
        "TYPE_CLASS",
        "TYPE_GLYPH",
        "label_stage_type",
        "type_class",
        "type_glyph",
    }
)
_TYPE_FIELDS = frozenset({"type", "stage_type"})


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


def test_every_stage_type_has_a_display_label() -> None:
    missing = find_types_missing_from(TYPE_LABEL)
    assert not missing, (
        f"{missing} have no TYPE_LABEL entry (app/web/diagrams.py), so their type tag "
        "prints the raw slug at a reader who is a journalist, not an engineer."
    )


def test_the_label_filter_reads_an_enum_member_not_its_repr() -> None:
    # Bare `{{ stage.type }}` on a StageType renders "StageType.report".
    assert label_stage_type(StageType.human_review_queue) == "review queue"
    assert label_stage_type("human_review_queue") == "review queue"
    assert label_stage_type("not_a_stage_type") == "not_a_stage_type"


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


# ─── A complete map nobody reads is still a slug on screen ────────────────────
# The rules above prove TYPE_LABEL covers every stage type. These two prove the
# reader-facing surfaces go through it: `f"{stype}"` in a node label and
# `{{ stage.type }}` in a template both print the slug past a map that was right
# all along — the workflow graph shipped exactly that.


def find_python_renders_of_a_raw_stage_type(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        tree = parse_module(path)
        carriers = _collect_stage_type_carriers(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.FormattedValue) and _renders_a_raw_slug(node.value, carriers):
                offenders.append(f"{path}:{node.lineno}  f-string prints {ast.unparse(node.value)}")
    return sorted(offenders)


def find_template_renders_of_a_raw_stage_type(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in _JINJA_OUTPUT.finditer(text):
            expression = match.group(1)
            if not _TEMPLATE_STAGE_TYPE_READ.search(expression):
                continue
            if any(route in expression for route in _PRESENTATION_ROUTES):
                continue
            if _sits_inside_a_tag(text, match.start()):
                continue
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path}:{line}  prints {{{{{expression}}}}}")
    return sorted(offenders)


def test_no_python_surface_prints_a_raw_stage_type() -> None:
    paths = find_source_files_under(_WEB_ROOT)
    offenders = find_python_renders_of_a_raw_stage_type(paths)
    assert not offenders, (
        "a stage type reaches the screen without a label — route it through "
        "TYPE_LABEL (app/web/diagrams.py):\n  " + "\n  ".join(offenders)
    )


def test_no_template_surface_prints_a_raw_stage_type() -> None:
    paths = sorted(_TEMPLATE_ROOT.glob("*.html"))
    assert paths, f"no templates under {_TEMPLATE_ROOT} — this rule would be vacuous"
    offenders = find_template_renders_of_a_raw_stage_type(paths)
    assert not offenders, (
        "a stage type reaches the reader without a label — add `| label_stage_type`:\n  "
        + "\n  ".join(offenders)
    )


def test_the_raw_slug_rules_can_see_a_violation(tmp_path: Path) -> None:
    module = tmp_path / "surface.py"
    module.write_text(
        "def render(node):\n"
        "    stype = node['type']\n"
        "    css = TYPE_CLASS.get(stype, 'custom')\n"
        "    labelled = TYPE_LABEL.get(stype, stype)\n"
        "    return f'<b>{stype}</b> {labelled} {css}'\n",
        encoding="utf-8",
    )
    template = tmp_path / "surface.html"
    template.write_text(
        '<span title="{{ stage.type }}">{{ stage.type | label_stage_type }}</span>\n'
        "<p>{{ stage.type }}</p>\n",
        encoding="utf-8",
    )
    python_offenders = find_python_renders_of_a_raw_stage_type([module])
    template_offenders = find_template_renders_of_a_raw_stage_type([template])
    assert [o.rsplit("  ", 1)[1] for o in python_offenders] == ["f-string prints stype"]
    assert [o.rsplit("  ", 1)[1] for o in template_offenders] == ["prints {{ stage.type }}"]


# A `{{ }}` between a `<` and its `>` is an attribute value: a CSS class, a URL, a
# data- payload, or the `title=` slug that names the id the config and manifest use
# beside the label. Reader prose is what sits in text position.
_JINJA_OUTPUT = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_TEMPLATE_STAGE_TYPE_READ = re.compile(
    r"\bstage\w*(?:\.type\b|\[['\"]type['\"]\])|\bstage_type\b"
)


def _sits_inside_a_tag(text: str, index: int) -> bool:
    return text.rfind("<", 0, index) > text.rfind(">", 0, index)


def _collect_stage_type_carriers(tree: ast.Module) -> set[str]:
    """Names holding a stage type — a column's `type` earns no entry, so it is never flagged."""
    routed = _collect_names_reaching_a_presentation_route(tree)
    carriers: set[str] = set()
    for name, value in _iter_name_bindings(tree):
        owner = _read_owner_of_a_type_field(value)
        if owner is not None and (name in routed or "stage" in owner):
            carriers.add(name)
    return carriers


def _renders_a_raw_slug(expression: ast.expr, carriers: set[str]) -> bool:
    if _routes_through_presentation(expression):
        return False
    owner = _read_owner_of_a_type_field(expression)
    if owner is not None and "stage" in owner:
        return True
    return any(
        isinstance(node, ast.Name) and node.id in carriers for node in ast.walk(expression)
    )


def _routes_through_presentation(expression: ast.expr) -> bool:
    return any(
        (isinstance(node, ast.Name) and node.id in _PRESENTATION_ROUTES)
        or (isinstance(node, ast.Attribute) and node.attr in _PRESENTATION_ROUTES)
        for node in ast.walk(expression)
    )


def _collect_names_reaching_a_presentation_route(tree: ast.Module) -> set[str]:
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call, ast.Subscript)) and _routes_through_presentation(node):
            reached |= {
                inner.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Name) and inner.id not in _PRESENTATION_ROUTES
            }
    return reached


def _read_owner_of_a_type_field(expression: ast.expr) -> str | None:
    """The object a `type`/`stage_type` field is read off — `n` in `n["type"]`; None if none is."""
    if isinstance(expression, ast.Attribute) and expression.attr in _TYPE_FIELDS:
        return ast.unparse(expression.value)
    if (
        isinstance(expression, ast.Subscript)
        and isinstance(expression.slice, ast.Constant)
        and expression.slice.value in _TYPE_FIELDS
    ):
        return ast.unparse(expression.value)
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr == "get"
        and expression.args
        and isinstance(expression.args[0], ast.Constant)
        and expression.args[0].value in _TYPE_FIELDS
    ):
        return ast.unparse(expression.func.value)
    return None


def _iter_name_bindings(tree: ast.Module) -> list[tuple[str, ast.expr]]:
    bindings: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            bindings += [
                (target.id, node.value) for target in node.targets if isinstance(target, ast.Name)
            ]
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            bindings.append((node.target.id, node.value))
    return bindings


def collect_authored_code_blocks() -> set[type]:
    """The block class each stage type hands back from find_authored_code_block."""
    blocks = set()
    for stage_cls in get_args(get_args(Stage)[0]):
        hook = stage_cls.find_authored_code_block
        if hook is AbstractStage.find_authored_code_block:
            continue
        blocks.add(get_type_hints(hook)["return"])
    return blocks


def test_every_authored_code_block_has_screen_copy() -> None:
    missing = sorted(b.__name__ for b in collect_authored_code_blocks() - set(BLOCK_COPY))
    assert not missing, (
        f"{missing} have no BLOCK_COPY entry (app/web/authored_code.py), so a stage "
        f"carrying one renders a headless code section with no summary and no code."
    )
