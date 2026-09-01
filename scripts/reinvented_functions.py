"""Per-PR report: a function this branch adds whose body already exists elsewhere.

Bodies are compared after alpha-renaming locals and arguments and erasing literal values,
so a copy that renamed everything still matches. Diff-scoped on purpose: the repo is full
of deliberate symmetry (archive/unarchive, one validator per stage type), and only a
clone of something that was ALREADY there is the reviewable event.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel

from scripts.lexicon import find_scanned_files, parse_source

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MARKER = "<!-- reinvented-functions -->"

# Below this a body is a guard or a one-line delegation, and alpha-renaming makes
# unrelated functions collide. Measured: at 12 nodes `_is_str_cell` hashes as `add_stage`.
_SMALLEST_COMPARABLE_BODY = 30


class Site(BaseModel):
    path: str
    name: str
    line: int
    nodes: int


class ShapeSnapshot(BaseModel):
    """Every comparable function body, keyed by the hash of its alpha-renamed shape."""

    sites: dict[str, list[Site]]
    functions: int


class Reinvention(BaseModel):
    added: Site
    existing: list[Site]


def build_snapshot(root: Path) -> ShapeSnapshot:
    sites: dict[str, list[Site]] = defaultdict(list)
    functions = 0
    for path in find_scanned_files(root):
        relative = str(path.relative_to(root))
        for node in ast.walk(parse_source(path)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            functions += 1
            shape = describe_shape(node)
            if shape is None:
                continue
            sites[shape].append(
                Site(path=relative, name=node.name, line=node.lineno, nodes=_count_nodes(node))
            )
    return ShapeSnapshot(sites=dict(sorted(sites.items())), functions=functions)


def describe_shape(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """None when the body is too small to compare without colliding."""
    body = [statement for statement in node.body if not _is_docstring(statement)]
    if not body or _count_nodes(node) < _SMALLEST_COMPARABLE_BODY:
        return None
    renamed: dict[str, str] = {}
    parts: list[str] = []
    for statement in body:
        _describe_node(statement, renamed, parts)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def find_reinventions(head: ShapeSnapshot, base: ShapeSnapshot) -> list[Reinvention]:
    found = []
    for shape, sites in head.sites.items():
        settled = {(site.path, site.name) for site in base.sites.get(shape, [])}
        # A rename empties this: the base site is gone from head, so there is nothing
        # left to call and the one remaining copy is not a duplicate of anything.
        survivors = [site for site in sites if (site.path, site.name) in settled]
        if not survivors:
            continue
        found += [
            Reinvention(added=site, existing=survivors)
            for site in sites
            if (site.path, site.name) not in settled
        ]
    return sorted(found, key=lambda found: (-found.added.nodes, found.added.path))


def render_markdown(head: ShapeSnapshot, base: ShapeSnapshot) -> str:
    lines = [_MARKER, "## Reinvented functions", ""]
    reinventions = find_reinventions(head, base)
    if reinventions:
        lines += ["| Added | Same body already in | Size |", "|---|---|---|"]
        lines += [
            f"| `{r.added.path}:{r.added.line}` `{r.added.name}` "
            f"| {', '.join(f'`{s.path}` `{s.name}`' for s in r.existing[:3])} "
            f"| {r.added.nodes} nodes |"
            for r in reinventions
        ]
        lines += [
            "",
            "Each body matches an existing one after renaming. Call the existing function,",
            "or say in review why the two must diverge — deliberate symmetry is a fine answer.",
        ]
    else:
        lines.append("This branch adds no function whose body already exists elsewhere.")
    lines += ["", f"{head.functions} functions scanned ({head.functions - base.functions:+d})."]
    return "\n".join(lines)


def render_annotations(reinventions: list[Reinvention]) -> list[str]:
    return [
        f"::warning file={r.added.path},line={r.added.line}::{r.added.name} has the same body as "
        f"{r.existing[0].name} in {r.existing[0].path}"
        for r in reinventions
    ]


def _describe_node(node: ast.AST, renamed: dict[str, str], parts: list[str]) -> None:
    if isinstance(node, ast.Name):
        parts.append("name:" + renamed.setdefault(node.id, f"v{len(renamed)}"))
        return
    if isinstance(node, ast.arg):
        parts.append("arg:" + renamed.setdefault(node.arg, f"v{len(renamed)}"))
        return
    if isinstance(node, ast.Constant):
        parts.append("const:" + type(node.value).__name__)
        return
    # The attribute name survives renaming: `.save()` and `.delete()` are different bodies.
    parts.append("attr:" + node.attr if isinstance(node, ast.Attribute) else type(node).__name__)
    for child in ast.iter_child_nodes(node):
        _describe_node(child, renamed, parts)


def _is_docstring(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)


def _count_nodes(node: ast.AST) -> int:
    return sum(1 for _ in ast.walk(node))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--markdown", nargs=2, metavar=("HEAD_JSON", "BASE_JSON"))
    parser.add_argument("--check", nargs=2, metavar=("HEAD_JSON", "BASE_JSON"))
    args = parser.parse_args(argv)
    if args.markdown:
        head, base = _read_pair(args.markdown)
        print(render_markdown(head, base))
        return 0
    if args.check:
        head, base = _read_pair(args.check)
        reinventions = find_reinventions(head, base)
        for annotation in render_annotations(reinventions):
            print(annotation)
        return 1 if reinventions else 0
    print(json.dumps(build_snapshot(args.root).model_dump(), indent=1, sort_keys=True))
    return 0


def _read_pair(paths: list[str]) -> tuple[ShapeSnapshot, ShapeSnapshot]:
    head, base = (ShapeSnapshot.model_validate_json(Path(p).read_text(encoding="utf-8")) for p in paths)
    return head, base


if __name__ == "__main__":
    sys.exit(main())
