"""Architecture: generic-infrastructure layers stay free of app-domain nouns.

A table of ``LayerVocabularyRule`` rows names an infrastructure target (a file
or a package directory) and the domain vocabulary it must never mention:

- ``app/core/persistence``, ``app/core/llm``, and ``app/core/agent`` are
  generic infra — a document store, an LLM option menu, and a reusable
  chat/agent engine. None of them may know the app's own nouns (project,
  workflow, stage, methodology, eval); knowing one would tie a reusable layer
  to a single caller.
- ``app/runtime/runner.py`` orchestrates a run generically: it must not touch
  connector-param keys like ``"path"`` — those belong to the stage modules
  that read them (``stages/input_data.py``) and to the ``Connector`` model
  that validates them. Absorbs the token-ban half of the old
  ``test_runner_binding_agnostic.py``; the remaining param keys it checked
  (format/file/list_columns/parse_dates) stay in that file, since this table
  only carries "path".

Matching is AST-based and word-segment-aware: a banned token must match a
whole segment of a snake_case/CamelCase identifier or string-literal dict key
("path" matches "file_path", not "pathway"). Docstrings and comments are not
identifiers or dict keys, so they are exempt without special-casing.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from arch._helpers import parse_module

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXEMPT_DIR_NAMES = {"tests", "_arch_tests", "__pycache__"}

# A capital letter preceded by a lowercase/digit, or a capital letter that
# starts a new word before a lowercase run (handles runs of capitals like an
# acronym), is a CamelCase word boundary.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_into_word_segments(identifier: str) -> list[str]:
    """Lowercase word segments of a snake_case/CamelCase identifier.

    "file_path" -> ["file", "path"]; "WorkflowVersion" -> ["workflow",
    "version"]; "pathway" -> ["pathway"] (one segment — never splits inside a
    word, so a banned token only matches a whole segment).
    """
    segments: list[str] = []
    for part in identifier.split("_"):
        if part:
            segments.extend(_CAMEL_BOUNDARY.sub("_", part).split("_"))
    return [segment.lower() for segment in segments if segment]


def find_banned_vocabulary_uses(
    tree: ast.Module,
    banned_tokens: frozenset[str],
    *,
    match_identifiers: bool = True,
) -> list[tuple[int, str]]:
    """(lineno, name) for every identifier or string-literal dict key in
    `tree` whose word segments include one of `banned_tokens`.

    Identifiers checked: function/class/variable/parameter names, attribute
    access (`x.attr`), and call-site keyword-argument names (`foo(k=1)`).
    Dict keys checked: a subscript (`x["k"]`), a
    `.get("k", ...)` first argument, and a dict-literal key. Set
    `match_identifiers=False` to check only dict keys — appropriate when the
    banned token also names a stdlib/generic concept (e.g. ``pathlib.Path``)
    that would otherwise swamp the identifier check with unrelated hits.
    """
    offenders: list[tuple[int, str]] = []
    if match_identifiers:
        offenders.extend(_find_banned_identifiers(tree, banned_tokens))
    offenders.extend(_find_banned_string_dict_keys(tree, banned_tokens))
    return offenders


def find_source_files(target: Path) -> list[Path]:
    """The .py files a rule's target covers: itself if `target` is a file, or
    every non-exempt .py file below it (skipping tests/ and _arch_tests/) if
    `target` is a directory."""
    if target.is_file():
        return [target]
    return sorted(
        path
        for path in target.rglob("*.py")
        if not any(part in _EXEMPT_DIR_NAMES for part in path.relative_to(target).parts)
    )


def _find_banned_identifiers(tree: ast.Module, banned_tokens: frozenset[str]) -> list[tuple[int, str]]:
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        identifier = _extract_identifier(node)
        if identifier is None:
            continue
        name, lineno = identifier
        if banned_tokens & set(split_into_word_segments(name)):
            offenders.append((lineno, name))
    return offenders


def _extract_identifier(node: ast.AST) -> tuple[str, int] | None:
    """The one name this AST node introduces or references, if any."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name, node.lineno
    if isinstance(node, ast.arg):
        return node.arg, node.lineno
    if isinstance(node, ast.Name):
        return node.id, node.lineno
    if isinstance(node, ast.Attribute):
        return node.attr, node.lineno
    if isinstance(node, ast.keyword) and node.arg is not None:
        return node.arg, node.lineno
    return None


def _find_banned_string_dict_keys(tree: ast.Module, banned_tokens: frozenset[str]) -> list[tuple[int, str]]:
    offenders: list[tuple[int, str]] = []
    for lineno, key in _iter_string_dict_keys(tree):
        if banned_tokens & set(split_into_word_segments(key)):
            offenders.append((lineno, key))
    return offenders


def _iter_string_dict_keys(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """(lineno, key) for every string-literal dict key: a subscript
    (`x["k"]`), a `.get("k", ...)` first argument, or a dict-literal key."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            yield node.lineno, node.slice.value
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.lineno, node.args[0].value
        elif isinstance(node, ast.Dict):
            for key_node in node.keys:
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    yield key_node.lineno, key_node.value


@dataclass(frozen=True)
class LayerVocabularyRule:
    """One row: an infrastructure target (file or package) and the app-domain
    vocabulary it must never mention.

    Set ``match_identifiers=False`` when a banned token also names a stdlib
    or common-word concept (e.g. ``pathlib.Path``, the "path" row below) that
    would otherwise swamp the identifier check with unrelated hits; dict-key
    matching still applies."""

    target: Path
    banned_tokens: frozenset[str]
    rationale: str
    match_identifiers: bool = True
    # Pre-existing offenders that are not the leak this rule targets (see the
    # row's rationale for why). A ratchet: new entries are forbidden — a new
    # offender must be fixed, not added here.
    allowlist: frozenset[str] = field(default_factory=frozenset)


_CORE_INFRA_TOKENS = frozenset({"project", "workflow", "stage", "methodology", "eval"})

_RULES: tuple[LayerVocabularyRule, ...] = (
    LayerVocabularyRule(
        target=_REPO_ROOT / "app" / "core" / "persistence.py",
        banned_tokens=_CORE_INFRA_TOKENS,
        rationale=(
            "app/core/persistence.py is the generic document store (any record "
            "type, keyed by collection); it must stay swappable for a record "
            "type it has never heard of, so it may not name one of ours."
        ),
    ),
    LayerVocabularyRule(
        target=_REPO_ROOT / "app" / "core" / "llm",
        banned_tokens=_CORE_INFRA_TOKENS,
        rationale=(
            "app/core/llm is the model menu and call-option types; a project/"
            "workflow/stage concept here would tie a reusable LLM boundary to "
            "one caller instead of any caller that names a model."
        ),
    ),
    LayerVocabularyRule(
        target=_REPO_ROOT / "app" / "core" / "agent",
        banned_tokens=_CORE_INFRA_TOKENS,
        rationale=(
            "app/core/agent is the reusable chat/agent engine (moved here by "
            "PR #143): it drives any tool-using conversation and must not "
            "assume it is editing a project's workflow or evaluating a stage."
        ),
    ),
    LayerVocabularyRule(
        target=_REPO_ROOT / "app" / "runtime" / "runner.py",
        banned_tokens=frozenset({"path"}),
        rationale=(
            "runner.py orchestrates a run generically; a connector-param key "
            "like \"path\" belongs to the stage modules that read it "
            "(stages/input_data.py) and the Connector model that validates it, "
            "not the orchestrator."
        ),
        # Identifiers are excluded here: this file imports pathlib.Path (a
        # stdlib type, not a domain noun) and uses local path-bookkeeping
        # variable names (e.g. constructing where to write an output file) for
        # its own manifest-writing, unrelated to connector-param semantics —
        # the token ban only targets dict keys read off a params mapping.
        match_identifiers=False,
        # "output_path"/"queue_path" are the runner's own manifest/queue
        # bookkeeping keys (where it wrote a completed stage's output, where a
        # halted stage's review queue file lives) — they end in the banned
        # segment "path" by coincidence of English, not a connector-param leak.
        allowlist=frozenset({"output_path", "queue_path"}),
    ),
)


def describe_rule_id(rule: LayerVocabularyRule) -> str:
    return rule.target.relative_to(_REPO_ROOT).as_posix()


@pytest.mark.parametrize("rule", _RULES, ids=describe_rule_id)
def test_infra_target_stays_free_of_banned_vocabulary(rule: LayerVocabularyRule) -> None:
    offenders = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}  {name!r}"
        for path in find_source_files(rule.target)
        for lineno, name in find_banned_vocabulary_uses(
            parse_module(path), rule.banned_tokens, match_identifiers=rule.match_identifiers
        )
        if name not in rule.allowlist
    ]
    assert not offenders, f"{rule.rationale}\n  " + "\n  ".join(offenders)


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_split_into_word_segments_splits_snake_case() -> None:
    assert split_into_word_segments("file_path") == ["file", "path"]


def test_split_into_word_segments_splits_camel_case() -> None:
    assert split_into_word_segments("WorkflowVersion") == ["workflow", "version"]


def test_split_into_word_segments_does_not_split_inside_a_word() -> None:
    assert split_into_word_segments("pathway") == ["pathway"]


def test_find_banned_vocabulary_uses_flags_banned_function_name() -> None:
    tree = ast.parse("def load_workflow_stage():\n    return 1\n")
    assert find_banned_vocabulary_uses(tree, frozenset({"workflow"})) == [(1, "load_workflow_stage")]


def test_find_banned_vocabulary_uses_flags_banned_parameter_and_attribute() -> None:
    tree = ast.parse("def go(project_id):\n    return project_id.stage\n")
    offenders = find_banned_vocabulary_uses(tree, frozenset({"project", "stage"}))
    assert {name for _, name in offenders} == {"project_id", "stage"}


def test_find_banned_vocabulary_uses_matches_whole_segment_only() -> None:
    tree = ast.parse("def pathway():\n    return 1\n")
    assert find_banned_vocabulary_uses(tree, frozenset({"path"})) == []


def test_find_banned_vocabulary_uses_ignores_docstrings_and_comments() -> None:
    tree = ast.parse(
        '"""A workflow-shaped docstring."""\n'
        "# a workflow comment\n"
        "def go():\n"
        "    return 1\n"
    )
    assert find_banned_vocabulary_uses(tree, frozenset({"workflow"})) == []


def test_find_banned_vocabulary_uses_flags_dict_literal_key() -> None:
    tree = ast.parse('d = {"project_id": 1}\n')
    assert find_banned_vocabulary_uses(tree, frozenset({"project"})) == [(1, "project_id")]


def test_find_banned_vocabulary_uses_flags_get_call_key() -> None:
    tree = ast.parse('params.get("path")\n')
    assert find_banned_vocabulary_uses(tree, frozenset({"path"})) == [(1, "path")]


def test_find_banned_vocabulary_uses_flags_subscript_key() -> None:
    tree = ast.parse('value = params["path"]\n')
    assert find_banned_vocabulary_uses(tree, frozenset({"path"})) == [(1, "path")]


def test_find_banned_vocabulary_uses_flags_call_keyword_argument_name() -> None:
    tree = ast.parse("foo(workflow_id=1)\n")
    assert find_banned_vocabulary_uses(tree, frozenset({"workflow"})) == [(1, "workflow_id")]


def test_find_banned_vocabulary_uses_ignores_clean_call_keyword_argument_name() -> None:
    tree = ast.parse("foo(record_id=1)\n")
    assert find_banned_vocabulary_uses(tree, frozenset({"workflow"})) == []


def test_find_banned_vocabulary_uses_can_skip_identifiers() -> None:
    tree = ast.parse("def read_path():\n    return 1\n")
    assert find_banned_vocabulary_uses(tree, frozenset({"path"}), match_identifiers=False) == []


def test_find_banned_vocabulary_uses_still_flags_keys_when_identifiers_skipped() -> None:
    tree = ast.parse('def read_path():\n    return params["path"]\n')
    offenders = find_banned_vocabulary_uses(tree, frozenset({"path"}), match_identifiers=False)
    assert offenders == [(2, "path")]


def test_find_banned_vocabulary_uses_ignores_clean_snippet() -> None:
    tree = ast.parse("def load_record(store):\n    return store.get('id')\n")
    assert find_banned_vocabulary_uses(tree, frozenset({"workflow", "project"})) == []


def test_find_source_files_returns_single_file_target(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n")
    assert find_source_files(target) == [target]


def test_find_source_files_walks_directory_excluding_tests_and_arch_tests(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "b.py").write_text("")
    (tmp_path / "_arch_tests").mkdir()
    (tmp_path / "_arch_tests" / "c.py").write_text("")
    assert {p.name for p in find_source_files(tmp_path)} == {"a.py"}
