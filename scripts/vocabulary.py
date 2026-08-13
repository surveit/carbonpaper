"""Per-PR vocabulary report: the words `app` uses outside its declared names.

`lexicon.py` covers what the code *declares* — types, fields, function tokens. This
covers what it *says*: local variable names, docstring prose, comment prose. Scoping to
`app` makes the world's proper nouns rare, not absent — `venezuela` is here once, in a
comment. Report-only, and a word it cannot place raises.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from collections import Counter, defaultdict
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from scripts.lexicon import find_scanned_files, parse_source, split_words

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MARKER = "<!-- vocabulary-report -->"
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")


class Surface(StrEnum):
    VARIABLE = "variable"
    DOCSTRING = "docstring"
    COMMENT = "comment"


class WordSurfaces(BaseModel):
    """Times a word was seen on each surface. Absent means never seen there."""

    variable: int = 0
    docstring: int = 0
    comment: int = 0

    def held(self) -> set[Surface]:
        return {surface for surface in Surface if getattr(self, surface.value) > 0}


class VocabularySnapshot(BaseModel):
    words: dict[str, WordSurfaces]
    comment_lines: int


class SurfaceGain(BaseModel):
    word: str
    surface: Surface
    is_new_word: bool


def build_snapshot(root: Path) -> VocabularySnapshot:
    seen: dict[str, Counter[Surface]] = defaultdict(Counter)
    comment_lines = 0
    for path in find_scanned_files(root):
        source = path.read_text(encoding="utf-8")
        _record_identifiers(parse_source(path), seen)
        comment_lines += _record_comments(source, seen)
    words = {
        word: WordSurfaces(
            variable=counts[Surface.VARIABLE],
            docstring=counts[Surface.DOCSTRING],
            comment=counts[Surface.COMMENT],
        )
        for word, counts in seen.items()
    }
    return VocabularySnapshot(words=dict(sorted(words.items())), comment_lines=comment_lines)


def find_surface_gains(head: VocabularySnapshot, base: VocabularySnapshot) -> list[SurfaceGain]:
    gains = [
        SurfaceGain(word=word, surface=surface, is_new_word=word not in base.words)
        for word, surfaces in head.words.items()
        for surface in sorted(surfaces.held() - base.words.get(word, WordSurfaces()).held())
    ]
    return sorted(gains, key=lambda gain: (gain.surface, not gain.is_new_word, gain.word))


def render_markdown(head: VocabularySnapshot, base: VocabularySnapshot) -> str:
    gains = find_surface_gains(head, base)
    if not gains:
        return f"{_MARKER}\n### 🟢 vocabulary — no new words\n\n`{len(head.words)}` words across variables, docstrings and comments · unchanged."
    lines = [_MARKER, f"### 🟡 vocabulary — {_render_headline(gains)}", ""]
    for surface in Surface:
        lines += _render_surface(surface, [gain for gain in gains if gain.surface is surface])
    lines += ["<sub>Report-only.</sub>", "", _render_totals(head, base)]
    return "\n".join(lines)


def _render_headline(gains: list[SurfaceGain]) -> str:
    words = len({gain.word for gain in gains if gain.is_new_word})
    moved = len({gain.word for gain in gains if not gain.is_new_word})
    headline = f"{words} new word{'s' if words != 1 else ''}" if words else "no new words"
    return f"{headline}, {moved} onto a new surface" if moved else headline


def _record_identifiers(tree: ast.Module, seen: dict[str, Counter[Surface]]) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            _count(seen, Surface.VARIABLE, split_words(node.id))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _record_signature(node, seen)


def _record_signature(node: ast.AST, seen: dict[str, Counter[Surface]]) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for argument in [*node.args.args, *node.args.kwonlyargs]:
            if argument.arg not in ("self", "cls"):
                _count(seen, Surface.VARIABLE, split_words(argument.arg))
    docstring = ast.get_docstring(node)  # type: ignore[arg-type]
    if docstring:
        _count(seen, Surface.DOCSTRING, [w.lower() for w in _WORD.findall(docstring)])


def _record_comments(source: str, seen: dict[str, Counter[Surface]]) -> int:
    lines = 0
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            lines += 1
            _count(seen, Surface.COMMENT, [w.lower() for w in _WORD.findall(token.string.lstrip("#"))])
    return lines


def _count(seen: dict[str, Counter[Surface]], surface: Surface, words: list[str]) -> None:
    for word in words:
        seen[word][surface] += 1


def _render_surface(surface: Surface, gains: list[SurfaceGain]) -> list[str]:
    if not gains:
        return []
    fresh = [gain for gain in gains if gain.is_new_word]
    moved = [gain for gain in gains if not gain.is_new_word]
    lines = [f"**{surface.value}** — {len(fresh)} new", ""]
    if fresh:
        lines += [_render_words(fresh), ""]
    if moved:
        lines += [f"already used elsewhere, now also here — {_render_words(moved)}", ""]
    return lines


def _render_words(gains: list[SurfaceGain]) -> str:
    return " ".join(f"`{gain.word}`" for gain in gains)


def _render_totals(head: VocabularySnapshot, base: VocabularySnapshot) -> str:
    return (
        "<details><summary>totals</summary>\n\n| | base | head |\n|---|---|---|\n"
        f"| words | {len(base.words)} | {len(head.words)} |\n"
        f"| comment lines | {base.comment_lines} | {head.comment_lines} |\n</details>"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--markdown", nargs=2, metavar=("HEAD_JSON", "BASE_JSON"))
    args = parser.parse_args(argv)
    if args.markdown:
        head, base = (
            VocabularySnapshot.model_validate_json(Path(p).read_text(encoding="utf-8")) for p in args.markdown
        )
        print(render_markdown(head, base))
        return 0
    print(json.dumps(build_snapshot(args.root).model_dump(), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
