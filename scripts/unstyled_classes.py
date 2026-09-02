"""Per-PR report: classes worn in markup that no stylesheet declares.

Such an element renders with no rules. The template is valid, the page returns 200, and
nothing in the suite sees it — the only witness is someone looking at the page. Vendored
assets are scanned like any other: counting their selectors can only suppress a finding,
never invent one.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MARKER = "<!-- unstyled-classes -->"
_SCANNED_SUFFIXES = (".html", ".css", ".js", ".py")
_EXEMPT_PARTS = {"__pycache__", "node_modules", "venv"}

_SELECTOR_BLOCK = re.compile(r"([^{}]+)\{")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CLASS_IN_SELECTOR = re.compile(r"\.([A-Za-z_][\w-]*)")
_CLASS_ATTRIBUTE = re.compile(r'class="([^"{}]*)"')
_ANY_CLASS_ATTRIBUTE = re.compile(r'class="[^"]*"')
_SELECTOR_MENTION = re.compile(r"[.#]([A-Za-z][\w-]*)")
_CLASS_LIST_CALL = re.compile(r"classList\.\w+\(['\"]([\w-]+)")

# A hook, not a style: the name is how a script or highlight.js finds the element.
_HOOK_PREFIXES = ("js-", "language-")


class UnstyledSnapshot(BaseModel):
    """Each class worn in markup that nothing styles, mapped to the files wearing it."""

    unstyled: dict[str, list[str]]
    declared: int
    worn: int


class Offender(BaseModel):
    name: str
    paths: list[str]


def build_snapshot(root: Path) -> UnstyledSnapshot:
    sources = read_scanned_files(root)
    declared = find_declared_classes(sources)
    referenced = find_referenced_names(sources)
    worn = find_worn_classes(sources)
    unstyled = {
        name: sorted(paths)
        for name, paths in worn.items()
        if name not in declared and name not in referenced and not name.startswith(_HOOK_PREFIXES)
    }
    return UnstyledSnapshot(unstyled=dict(sorted(unstyled.items())), declared=len(declared), worn=len(worn))


def find_new_offenders(head: UnstyledSnapshot, base: UnstyledSnapshot) -> list[Offender]:
    return [
        Offender(name=name, paths=paths) for name, paths in head.unstyled.items() if name not in base.unstyled
    ]


def render_markdown(head: UnstyledSnapshot, base: UnstyledSnapshot) -> str:
    lines = [_MARKER, "## Unstyled classes", ""]
    offenders = find_new_offenders(head, base)
    if offenders:
        lines += ["| Class | Worn in |", "|---|---|"]
        lines += [f"| `.{o.name}` | {', '.join(f'`{p}`' for p in o.paths)} |" for o in offenders]
        lines += ["", "No stylesheet declares these and no script reads them, so the elements", "render unstyled. Style them, or drop the attribute."]
    else:
        lines.append("This branch wears no class that nothing styles.")
    lines += ["", _render_totals(head, base)]
    return "\n".join(lines)


def render_annotations(offenders: list[Offender]) -> list[str]:
    return [
        f"::warning file={o.paths[0]}::.{o.name} is worn here but no stylesheet declares it"
        for o in offenders
    ]


def read_scanned_files(root: Path) -> dict[str, str]:
    app = root / "app"
    if not app.is_dir():
        raise FileNotFoundError(f"the report scans {app}, which does not exist")
    sources = {
        str(path.relative_to(root)): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(app.rglob("*"))
        if path.is_file() and path.suffix in _SCANNED_SUFFIXES and not _is_exempt(path)
    }
    if not sources:
        raise ValueError(f"no markup or stylesheet found under {app} — the scan is misconfigured")
    return sources


def find_declared_classes(sources: dict[str, str]) -> set[str]:
    declared: set[str] = set()
    for path, text in sources.items():
        if path.endswith(".css"):
            declared |= _classes_in_stylesheet(text)
        elif path.endswith(".html"):
            for block in re.findall(r"<style[^>]*>(.*?)</style>", text, flags=re.S):
                declared |= _classes_in_stylesheet(block)
    return declared


def find_referenced_names(sources: dict[str, str]) -> set[str]:
    """Any mention outside a class attribute — a script selector, a python string."""
    referenced: set[str] = set()
    for text in sources.values():
        referenced |= set(_SELECTOR_MENTION.findall(_ANY_CLASS_ATTRIBUTE.sub("", text)))
        referenced |= set(_CLASS_LIST_CALL.findall(text))
    return referenced


def find_worn_classes(sources: dict[str, str]) -> dict[str, set[str]]:
    worn: dict[str, set[str]] = {}
    for path, text in sources.items():
        if not path.endswith(".html"):
            continue
        for attribute in _CLASS_ATTRIBUTE.findall(text):
            for name in attribute.split():
                # A Jinja expression builds the name at render time; nothing static to check.
                if name and not name.startswith("{"):
                    worn.setdefault(name, set()).add(path)
    return worn


def _classes_in_stylesheet(text: str) -> set[str]:
    stripped = _CSS_COMMENT.sub("", text)
    return {name for selector in _SELECTOR_BLOCK.findall(stripped) for name in _CLASS_IN_SELECTOR.findall(selector)}


def _is_exempt(path: Path) -> bool:
    return any(part in _EXEMPT_PARTS or part.startswith(".") for part in path.parts)


def _render_totals(head: UnstyledSnapshot, base: UnstyledSnapshot) -> str:
    standing = len(head.unstyled)
    return (
        f"{head.worn} classes worn, {head.declared} declared, {standing} unstyled "
        f"({standing - len(base.unstyled):+d} on this branch)."
    )


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
        offenders = find_new_offenders(head, base)
        for annotation in render_annotations(offenders):
            print(annotation)
        return 1 if offenders else 0
    print(json.dumps(build_snapshot(args.root).model_dump(), indent=1, sort_keys=True))
    return 0


def _read_pair(paths: list[str]) -> tuple[UnstyledSnapshot, UnstyledSnapshot]:
    head, base = (UnstyledSnapshot.model_validate_json(Path(p).read_text(encoding="utf-8")) for p in paths)
    return head, base


if __name__ == "__main__":
    sys.exit(main())
