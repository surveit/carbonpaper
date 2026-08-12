"""Per-PR lexicon report: the vocabulary of `app`, and how a branch changes it.

A word's ROLE is read from where it sits in the AST, not from grammar — there is no
verb oracle, and `record`/`stage` prove one is impossible. The registry freezes the
role SET each word already occupies; a word gaining a role it has never held is the
reviewable event. Purely informational, so anything uncomputable raises rather than
surfacing as a wrong number in someone's PR comment.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MARKER = "<!-- lexicon-report -->"


class Role(StrEnum):
    VERB = "verb"
    NOUN = "noun"
    FIELD = "field"
    ACCESSOR = "accessor"


class WordRoles(BaseModel):
    """Times a word was seen in each role. A role absent here is one it has never held."""

    verb: int = 0
    noun: int = 0
    field: int = 0
    accessor: int = 0
    # Set by a human, never by the scan: this word reads as a noun, so its `verb`
    # count is grandfathered debt and may only fall.
    noun_led: bool = False

    def held(self) -> set[Role]:
        return {role for role in Role if getattr(self, role.value) > 0}


class Sighting(BaseModel):
    """Where a word first took a role. In the CI artifact only — line numbers churn."""

    path: str
    line: int
    source: str


class LexiconSnapshot(BaseModel):
    words: dict[str, WordRoles]
    functions: int
    accessors: int
    types: int
    sightings: dict[str, Sighting] = {}

    def sighting(self, word: str, role: Role) -> Sighting | None:
        return self.sightings.get(f"{word}:{role.value}")


class RoleGain(BaseModel):
    word: str
    role: Role
    is_new_word: bool


def build_snapshot(root: Path) -> LexiconSnapshot:
    seen: dict[str, Counter[Role]] = defaultdict(Counter)
    sightings: dict[str, Sighting] = {}
    functions = accessors = types = 0
    for path in find_scanned_files(root):
        source = path.read_text(encoding="utf-8")
        site = _Recorder(sightings, path.relative_to(root).as_posix(), source.splitlines())
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.ClassDef):
                types += _record_type(node, seen, site)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                role = _record_function(node, seen, site)
                functions += role is not None
                accessors += role is Role.ACCESSOR
    words = {
        word: WordRoles(
            verb=counts[Role.VERB],
            noun=counts[Role.NOUN],
            field=counts[Role.FIELD],
            accessor=counts[Role.ACCESSOR],
        )
        for word, counts in seen.items()
    }
    return LexiconSnapshot(
        words=dict(sorted(words.items())),
        functions=functions,
        accessors=accessors,
        types=types,
        sightings=dict(sorted(sightings.items())),
    )


def find_scanned_files(root: Path) -> list[Path]:
    """`app` only, minus the exemptions `arch.scope` owns — `_arch_tests/` above all."""
    app = root / "app"
    if not app.is_dir():
        raise FileNotFoundError(f"lexicon scans {app}, which does not exist")
    files = sorted(path for path in app.rglob("*.py") if _is_scanned(path.relative_to(app)))
    if not files:
        raise ValueError(f"lexicon found no source under {app} — the scope filter excludes everything")
    return files


def find_role_gains(head: LexiconSnapshot, base: LexiconSnapshot) -> list[RoleGain]:
    gains = [
        RoleGain(word=word, role=role, is_new_word=word not in base.words)
        for word, roles in head.words.items()
        for role in sorted(roles.held() - base.words.get(word, WordRoles()).held())
    ]
    return sorted(gains, key=lambda gain: (not gain.is_new_word, gain.word, gain.role))


def find_ratchet_breaks(head: LexiconSnapshot, registry: LexiconSnapshot) -> list[str]:
    """A word a human marked noun-led grew its verb count — the debt went up, not down."""
    return [
        f"{word} (verb {registry.words[word].verb} → {roles.verb})"
        for word, roles in head.words.items()
        if registry.words.get(word, WordRoles()).noun_led and roles.verb > registry.words[word].verb
    ]


class SourceLinks(BaseModel):
    """Where to point a sighting. Absent means render plain text — never a guessed URL."""

    repo: str
    sha: str

    def url(self, path: str, line: int) -> str:
        return f"https://github.com/{self.repo}/blob/{self.sha}/{path}#L{line}"


def render_markdown(head: LexiconSnapshot, base: LexiconSnapshot, links: SourceLinks | None = None) -> str:
    gains = find_role_gains(head, base)
    breaks = find_ratchet_breaks(head, base)
    if not gains and not breaks:
        return (
            f"{_MARKER}\n### 🟢 lexicon — no new vocabulary\n\n"
            f"`{len(head.words)}` words · `{head.types}` types · `{head.functions}` functions · unchanged."
        )
    lines = [_MARKER, f"### 🟡 lexicon — {len(gains) + len(breaks)} to look at", ""]
    if gains:
        lines += [
            "Each row is one question: **a new concept, or one you already have, respelled?**",
            "",
            "| word | gains role | held before | where |",
            "|---|---|---|---|",
            *[_render_gain(gain, base, head, links) for gain in gains],
            "",
        ]
    if breaks:
        lines += ["**Noun-led ratchet went backwards** — a word marked as reading like a", "noun took on more function names that lead with it.", ""]
        lines += [f"- `{item}`" for item in breaks]
        lines += [""]
    lines += [_render_registry_hint(), "", _render_totals(head, base)]
    return "\n".join(lines)


def parse_source(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def split_words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [word.lower() for word in spaced.split("_") if word and not word.isdigit()]


def is_accessor(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A dumb field accessor: `@property`, or a lone `return self.a.b` with nothing computed."""
    if any("property" in ast.unparse(d) for d in node.decorator_list):
        return True
    body = [s for s in node.body if not _is_docstring(s)]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return False
    return _is_attribute_chain(body[0].value)


_TYPE_BASES = re.compile(r"\b(BaseModel|PersistedModel|TypedDict|NamedTuple|Enum|Protocol|ABC)\b")
_EXEMPT_PARTS = {"_arch_tests", "__pycache__", "_vendor", "node_modules", "venv"}


def _is_scanned(relative: Path) -> bool:
    return not any(part.startswith(".") or part in _EXEMPT_PARTS for part in relative.parts)


class _Recorder:
    """Keeps the first place each (word, role) was seen while a file is walked."""

    def __init__(self, sightings: dict[str, Sighting], path: str, lines: list[str]) -> None:
        self._sightings, self._path, self._lines = sightings, path, lines

    def note(self, word: str, role: Role, lineno: int) -> None:
        key = f"{word}:{role.value}"
        if key in self._sightings:
            return
        text = self._lines[lineno - 1].strip() if 0 < lineno <= len(self._lines) else ""
        self._sightings[key] = Sighting(path=self._path, line=lineno, source=text[:110])


def _record_type(node: ast.ClassDef, seen: dict[str, Counter[Role]], site: _Recorder) -> bool:
    if not _is_declared_type(node):
        return False
    for word in split_words(node.name):
        seen[word][Role.NOUN] += 1
        site.note(word, Role.NOUN, node.lineno)
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            for word in split_words(statement.target.id):
                seen[word][Role.FIELD] += 1
                site.note(word, Role.FIELD, statement.lineno)
    return True


def _record_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, seen: dict[str, Counter[Role]], site: _Recorder
) -> Role | None:
    if node.name.startswith("__"):
        return None
    token = node.name.lstrip("_").split("_")[0]
    if not token:
        return None
    role = Role.ACCESSOR if is_accessor(node) else Role.VERB
    seen[token][role] += 1
    site.note(token, role, node.lineno)
    return role


def _is_declared_type(node: ast.ClassDef) -> bool:
    if any("dataclass" in ast.unparse(d) for d in node.decorator_list):
        return True
    return any(_TYPE_BASES.search(ast.unparse(base)) for base in node.bases)


def _is_attribute_chain(value: ast.expr) -> bool:
    while isinstance(value, ast.Attribute):
        value = value.value
    return isinstance(value, ast.Name)


def _is_docstring(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)


def _render_gain(
    gain: RoleGain, base: LexiconSnapshot, head: LexiconSnapshot, links: SourceLinks | None
) -> str:
    held = base.words.get(gain.word, WordRoles()).held()
    before = "**new word**" if gain.is_new_word else ", ".join(sorted(r.value for r in held))
    return f"| `{gain.word}` | `{gain.role.value}` | {before} | {_render_sighting(gain, head, links)} |"


def _render_sighting(gain: RoleGain, head: LexiconSnapshot, links: SourceLinks | None) -> str:
    seen = head.sighting(gain.word, gain.role)
    if seen is None:
        return "—"
    # `X | None` is everywhere here, and a bare pipe splits the markdown cell.
    source = seen.source.replace("|", "\\|")
    where = f"{seen.path}:{seen.line}"
    located = where if links is None else f"[{where}]({links.url(seen.path, seen.line)})"
    return f"`{source}`<br><sub>{located}</sub>"


def _render_registry_hint() -> str:
    return (
        "<sub>Report-only. To accept these, add them to `lexicon.json` in this PR — "
        "that diff <i>is</i> the review.</sub>"
    )


def _render_totals(head: LexiconSnapshot, base: LexiconSnapshot) -> str:
    rows = "\n".join(
        f"| {label} | {getattr(base, field)} | {getattr(head, field)} |"
        for label, field in (("types", "types"), ("functions", "functions"), ("accessors", "accessors"))
    )
    return (
        "<details><summary>registry totals</summary>\n\n"
        f"| | base | head |\n|---|---|---|\n| words | {len(base.words)} | {len(head.words)} |\n{rows}\n"
        "</details>"
    )


def _read_links(repo: str | None, sha: str | None) -> SourceLinks | None:
    if repo and sha:
        return SourceLinks(repo=repo, sha=sha)
    if repo or sha:
        raise ValueError("--repo and --sha go together: half of them would build a wrong URL")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--markdown", nargs=2, metavar=("HEAD_JSON", "BASE_JSON"))
    parser.add_argument(
        "--registry", action="store_true", help="drop sightings: line numbers churn the committed file"
    )
    parser.add_argument("--repo", help="OWNER/NAME, to link each sighting at --sha")
    parser.add_argument("--sha", help="commit the links resolve against; both or neither")
    args = parser.parse_args(argv)
    if args.markdown:
        links = _read_links(args.repo, args.sha)  # bad arguments fail before any file is read
        head, base = (LexiconSnapshot.model_validate_json(Path(p).read_text(encoding="utf-8")) for p in args.markdown)
        print(render_markdown(head, base, links))
        return 0
    snapshot = build_snapshot(args.root)
    if args.registry:
        snapshot = snapshot.model_copy(update={"sightings": {}})
    print(json.dumps(snapshot.model_dump(), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
