"""Grounding for the lexicon scan: an extractor that silently finds nothing reports a
clean registry, which is the one failure mode that makes the gate worse than absent.
Every assertion here names a real artifact in `app/`, so the scan going blind is a red
test rather than a green one.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.lexicon import (
    LexiconSnapshot,
    Role,
    WordRoles,
    build_snapshot,
    find_ratchet_breaks,
    find_role_gains,
    find_scanned_files,
    is_accessor,
    split_words,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def snapshot() -> LexiconSnapshot:
    return build_snapshot(_REPO_ROOT)


def parse_function(source: str) -> ast.FunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


# --- the scan reaches real code -------------------------------------------------


def test_scan_finds_the_app_package() -> None:
    files = find_scanned_files(_REPO_ROOT)
    assert len(files) > 100, f"only {len(files)} files scanned — the filter is too wide"


def test_scan_excludes_colocated_arch_tests() -> None:
    assert not [path for path in find_scanned_files(_REPO_ROOT) if "_arch_tests" in path.parts]


def test_arch_test_functions_contribute_no_verb_role(snapshot: LexiconSnapshot) -> None:
    """`test` leads ~50 names under `_arch_tests/`; in scope they would drown the registry."""
    assert snapshot.words["test"].verb == 0


# --- every declared-type family is actually recognised ---------------------------


@pytest.mark.parametrize(
    ("word", "role"),
    [
        ("workflow", Role.NOUN),  # WorkflowVersion, a pydantic model
        ("graph", Role.NOUN),  # StageInGraph, a Protocol
        ("status", Role.NOUN),  # RunStatus, an Enum
        ("card", Role.NOUN),  # ProjectCard, a dataclass
        ("id", Role.FIELD),  # stage_id and friends
        ("find", Role.VERB),  # the most common leading token
        ("id", Role.ACCESSOR),  # WorkflowStage.id returns self.stage.id
    ],
)
def test_known_artifact_holds_its_role(snapshot: LexiconSnapshot, word: str, role: Role) -> None:
    assert role in snapshot.words[word].held(), f"scan lost {word!r} in role {role.value}"


def test_totals_are_in_the_expected_neighbourhood(snapshot: LexiconSnapshot) -> None:
    assert snapshot.types > 100
    assert snapshot.functions > 1000
    assert 0 < snapshot.accessors < snapshot.functions // 10


# --- the accessor predicate draws the line where the naming rule does ------------


@pytest.mark.parametrize(
    "source",
    [
        "def id(self) -> StageId:\n    return self.stage.id",
        "def name(self) -> str:\n    return self._name",
        "@property\ndef total(self) -> int:\n    return sum(self.parts)",
        'def kind(self) -> str:\n    """Doc."""\n    return self._kind',
    ],
)
def test_accessor_is_recognised(source: str) -> None:
    assert is_accessor(parse_function(source))


@pytest.mark.parametrize(
    "source",
    [
        # One line, returns — but computes. The naming rule wants a verb here.
        "def find_stage(self, key):\n    return next(s for s in self.stages if s.id == key)",
        "def build_view(self):\n    return StageView(stage=self.stage)",
        "def read_rows(self):\n    return self.frame.to_dict()",
        "def save(self) -> None:\n    self.store.write(self.payload)",
    ],
)
def test_action_function_is_not_an_accessor(source: str) -> None:
    assert not is_accessor(parse_function(source))


# --- role gains and the noun-led ratchet ----------------------------------------


def test_role_gain_is_reported_for_a_word_in_a_new_role() -> None:
    base = LexiconSnapshot(words={"id": WordRoles(field=48)}, functions=1, accessors=0, types=1)
    head = LexiconSnapshot(words={"id": WordRoles(field=48, noun=1)}, functions=1, accessors=0, types=2)
    assert [(g.word, g.role, g.is_new_word) for g in find_role_gains(head, base)] == [("id", Role.NOUN, False)]


def test_a_word_staying_in_roles_it_already_held_is_silent() -> None:
    base = LexiconSnapshot(words={"run": WordRoles(verb=44, noun=20)}, functions=1, accessors=0, types=1)
    head = LexiconSnapshot(words={"run": WordRoles(verb=99, noun=88)}, functions=1, accessors=0, types=1)
    assert find_role_gains(head, base) == []


def test_noun_led_word_growing_its_verb_count_breaks_the_ratchet() -> None:
    registry = LexiconSnapshot(words={"stage": WordRoles(verb=15, noun_led=True)}, functions=1, accessors=0, types=1)
    head = LexiconSnapshot(words={"stage": WordRoles(verb=16)}, functions=1, accessors=0, types=1)
    assert find_ratchet_breaks(head, registry) == ["stage (verb 15 → 16)"]
    shrunk = LexiconSnapshot(words={"stage": WordRoles(verb=14)}, functions=1, accessors=0, types=1)
    assert find_ratchet_breaks(shrunk, registry) == []


# --- the committed registry matches the tree it describes ------------------------


def test_committed_registry_is_current(snapshot: LexiconSnapshot) -> None:
    registry = LexiconSnapshot.model_validate_json((_REPO_ROOT / "lexicon.json").read_text(encoding="utf-8"))
    assert find_role_gains(snapshot, registry) == [], "lexicon.json is stale — re-mint it in this PR"


def test_split_words_handles_both_casings() -> None:
    assert split_words("WorkflowStageInput") == ["workflow", "stage", "input"]
    assert split_words("output_schema") == ["output", "schema"]
