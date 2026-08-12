"""Grounding for the lexicon scan: an extractor that silently finds nothing reports a
clean registry, which is the one failure mode that makes the gate worse than absent.
Every assertion here names a real artifact in `app/`, so the scan going blind is a red
test rather than a green one.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts.lexicon import (
    LexiconSnapshot,
    Sighting,
    Role,
    WordRoles,
    build_snapshot,
    find_ratchet_breaks,
    find_role_gains,
    find_scanned_files,
    is_accessor,
    main,
    render_markdown,
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


def read_registry() -> LexiconSnapshot:
    return LexiconSnapshot.model_validate_json((_REPO_ROOT / "lexicon.json").read_text(encoding="utf-8"))


def test_registry_still_describes_a_real_tree(snapshot: LexiconSnapshot) -> None:
    # Not equality: that would force a PR adding a word to re-mint, accepting it silently.
    registry = read_registry()
    assert set(registry.words) - set(snapshot.words) == set(), "registry names words the tree lost"


def test_noun_led_ratchet_holds(snapshot: LexiconSnapshot) -> None:
    assert find_ratchet_breaks(snapshot, read_registry()) == []


def test_noun_led_words_are_marked_by_hand_only() -> None:
    marked = {word for word, roles in read_registry().words.items() if roles.noun_led}
    assert marked == {"stage", "row"}, "a noun_led mark is a human decision, not a scan output"


def test_split_words_handles_both_casings() -> None:
    assert split_words("WorkflowStageInput") == ["workflow", "stage", "input"]
    assert split_words("output_schema") == ["output", "schema"]


# --- sightings: the snippet the report prints -----------------------------------


def test_sighting_points_at_real_source(snapshot: LexiconSnapshot) -> None:
    seen = snapshot.sighting("find", Role.VERB)
    assert seen is not None
    assert seen.path.startswith("app/") and seen.line > 0
    assert seen.source.lstrip().startswith(("def ", "async def "))


def test_every_gain_the_report_prints_can_be_located(snapshot: LexiconSnapshot) -> None:
    """A row with no snippet is a row the reader has to go hunt for by hand."""
    missing = [
        f"{word}:{role.value}"
        for word, roles in snapshot.words.items()
        for role in roles.held()
        if snapshot.sighting(word, role) is None
    ]
    assert missing == [], f"{len(missing)} word-roles have no sighting"


def test_sighting_source_is_bounded() -> None:
    """A long line would blow out the table; 110 chars keeps a row readable."""
    seen = Sighting(path="app/x.py", line=1, source="x" * 110)
    assert len(seen.source) <= 110


def test_rendered_row_carries_the_snippet() -> None:
    base = LexiconSnapshot(words={}, functions=0, accessors=0, types=0)
    head = LexiconSnapshot(
        words={"sidecar": WordRoles(verb=1)},
        functions=1,
        accessors=0,
        types=0,
        sightings={"sidecar:verb": Sighting(path="app/runtime/trace.py", line=222, source="def _sidecar(self):")},
    )
    body = render_markdown(head, base)
    assert "def _sidecar(self):" in body
    assert "app/runtime/trace.py:222" in body


def test_registry_flag_drops_sightings(capsys: pytest.CaptureFixture[str]) -> None:
    """Committing line numbers would double the file and churn it on every edit."""
    main(["--registry"])
    assert json.loads(capsys.readouterr().out)["sightings"] == {}


def test_a_union_type_does_not_split_the_table_cell() -> None:
    """`X | None` is the commonest annotation here; a bare pipe silently breaks the row."""
    base = LexiconSnapshot(words={}, functions=0, accessors=0, types=0)
    head = LexiconSnapshot(
        words={"refused": WordRoles(field=1)},
        functions=0,
        accessors=0,
        types=1,
        sightings={"refused:field": Sighting(path="app/x.py", line=9, source="refused: str | None")},
    )
    row = next(line for line in render_markdown(head, base).split("\n") if "refused" in line)
    assert row.count("|") - row.count("\\|") == 5, f"cell count wrong: {row}"
    assert "app/x.py:9" in row
