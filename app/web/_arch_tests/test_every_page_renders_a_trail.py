"""Architecture: a page template renders the header trail, and never discards one.

A page extending base.html with no `breadcrumbs` block silently gets the bare brand; a
route may also pass `crumbs` to a template that cannot draw them, which lineage.html did.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from arch import find_governed_files
from arch._helpers import parse_module

_APP = Path(__file__).resolve().parents[3] / "app"
# Two Jinja roots: the app's own, and the chat surface's (app/web/chat_router.py builds
# its own environment). A name in neither is a typo, not an exemption.
_TEMPLATE_ROOTS = (_APP / "templates", _APP / "web" / "chat_templates")
_TRAIL_PARTIAL = "_breadcrumbs.html"
_EXTENDS = re.compile(r'{%\s*extends\s*"([^"]+)"')
_RENDERS_TRAIL = re.compile(r'{%\s*include\s*"' + re.escape(_TRAIL_PARTIAL) + r'"')

# Pages that legitimately carry no trail, each with the reason. A NEW page belongs in
# neither list — give it a trail instead. This dict may only shrink.
_NO_TRAIL: dict[str, str] = {
    "index.html": "the project list IS the trail's home rung; a trail to itself says nothing",
    "admin.html": "sits outside the project hierarchy, reached from the header's own link",
    "packet_index.html": "review packet — an offline artifact whose links must not leave it",
    "packet_stage.html": "review packet — same, and it carries its own back link to the index",
    "chat.html": "the agent surface, addressed by session id rather than by project",
    "chat_index.html": "same",
}


def test_every_page_template_renders_the_trail() -> None:
    offenders = [
        f"{name} — extends {_ancestry(name)[-1] if _ancestry(name) else '(nothing)'}, "
        "renders no trail and is not in _NO_TRAIL"
        for name in _page_templates()
        if not _resolves_a_trail(name) and name not in _NO_TRAIL
    ]
    assert not offenders, (
        f"a page must render the header trail — include {_TRAIL_PARTIAL} (directly, or by "
        "extending a template that does) and pass `crumbs` from the route. Falling through "
        "to base.html's bare brand is how per-page back links came back last time:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_no_route_passes_crumbs_a_template_will_discard() -> None:
    """The lineage bug: a computed trail handed to a template that cannot draw it."""
    offenders = [
        f"{template} is rendered with `crumbs` but resolves no {_TRAIL_PARTIAL}"
        for template in sorted(_templates_rendered_with_crumbs())
        if not _resolves_a_trail(template)
    ]
    assert not offenders, (
        "these routes build a trail that is then thrown away — either render it in the "
        "template or stop computing it:\n  " + "\n  ".join(offenders)
    )


def test_the_no_trail_list_carries_no_stale_entry() -> None:
    stale = sorted(name for name in _NO_TRAIL if not _find(name))
    assert not stale, f"_NO_TRAIL names templates that no longer exist: {stale}"


def _page_templates() -> list[str]:
    """A whole page, not a fragment: `_`-prefixed is a partial, and routes do return those."""
    return sorted(n for n in _templates_rendered_by_routes() if not n.startswith("_"))


def _templates_rendered_by_routes() -> set[str]:
    return {name for name, _ in _template_response_calls()}


def _templates_rendered_with_crumbs() -> set[str]:
    return {name for name, keys in _template_response_calls() if "crumbs" in keys}


def _template_response_calls() -> list[tuple[str, set[str]]]:
    calls: list[tuple[str, set[str]]] = []
    for path in find_governed_files(__file__):
        for node in ast.walk(parse_module(path)):
            call = _as_template_response(node)
            if call is not None:
                calls.append(call)
    return calls


def _as_template_response(node: ast.AST) -> tuple[str, set[str]] | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr != "TemplateResponse":
        return None
    name = next(
        (a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)),
        None,
    )
    if name is None or not name.endswith(".html"):
        return None
    context = next((a for a in node.args if isinstance(a, ast.Dict)), None)
    keys = {
        k.value
        for k in (context.keys if context else [])
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }
    return name, keys


def _resolves_a_trail(name: str) -> bool:
    return any(_RENDERS_TRAIL.search(_read(step)) for step in [name, *_ancestry(name)])


def _ancestry(name: str) -> list[str]:
    chain: list[str] = []
    current = name
    while (match := _EXTENDS.search(_read(current))) is not None:
        current = match.group(1)
        if current in chain:  # a cycle would hang the walk; Jinja would refuse it anyway
            break
        chain.append(current)
    return chain


def _read(name: str) -> str:
    path = _find(name)
    return path.read_text(encoding="utf-8") if path is not None else ""


def _find(name: str) -> Path | None:
    return next((root / name for root in _TEMPLATE_ROOTS if (root / name).is_file()), None)
