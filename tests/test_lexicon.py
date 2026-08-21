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
    SourceLinks,
    Role,
    WordRoles,
    build_snapshot,
    find_new_verb_uses,
    find_ratchet_breaks,
    find_role_gains,
    find_scanned_files,
    is_accessor,
    main,
    read_registry,
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


def test_noun_led_ratchet_holds(snapshot: LexiconSnapshot) -> None:
    assert find_ratchet_breaks(snapshot, read_registry(_REPO_ROOT)) == []


def test_noun_led_words_are_marked_by_hand_only() -> None:
    marked = {word for word, roles in read_registry(_REPO_ROOT).words.items() if roles.noun_led}
    assert marked == {"stage", "row"}, "a noun_led mark is a human decision, not a scan output"


# --- new-verb-use check: wider than the ratchet, but never blocking -------------


def test_first_verb_use_of_a_noun_only_word_is_flagged() -> None:
    base = LexiconSnapshot(words={"figure": WordRoles(noun=12)}, functions=1, accessors=0, types=1)
    head = LexiconSnapshot(words={"figure": WordRoles(noun=12, verb=1)}, functions=1, accessors=0, types=1)
    assert find_new_verb_uses(head, base) == ["figure (verb 0 → 1)"]


def test_word_already_holding_verb_is_not_reflagged() -> None:
    base = LexiconSnapshot(words={"run": WordRoles(verb=3, noun=1)}, functions=1, accessors=0, types=1)
    head = LexiconSnapshot(words={"run": WordRoles(verb=9, noun=1)}, functions=1, accessors=0, types=1)
    assert find_new_verb_uses(head, base) == []


def test_noun_led_word_growing_its_verb_count_is_not_double_flagged_here() -> None:
    """`stage` already carries verb debt on base, so the ratchet test owns this case."""
    base = LexiconSnapshot(words={"stage": WordRoles(verb=15, noun_led=True)}, functions=1, accessors=0, types=1)
    head = LexiconSnapshot(words={"stage": WordRoles(verb=16)}, functions=1, accessors=0, types=1)
    assert find_new_verb_uses(head, base) == []


def test_brand_new_word_is_not_flagged() -> None:
    """A word absent from base is `find_role_gains` territory, not this check."""
    base = LexiconSnapshot(words={}, functions=0, accessors=0, types=0)
    head = LexiconSnapshot(words={"sidecar": WordRoles(verb=1)}, functions=1, accessors=0, types=0)
    assert find_new_verb_uses(head, base) == []


def test_pre_existing_drift_against_the_committed_registry_does_not_refire() -> None:
    """Diffing HEAD against itself must be silent — the check compares to BASE, not `lexicon.json`."""
    registry = read_registry(_REPO_ROOT)
    assert find_new_verb_uses(registry, registry) == []


def test_check_new_verbs_exits_nonzero_on_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_path, head_path = tmp_path / "base.json", tmp_path / "head.json"
    base_path.write_text(
        LexiconSnapshot(words={"figure": WordRoles(noun=1)}, functions=1, accessors=0, types=1).model_dump_json(),
        encoding="utf-8",
    )
    head_path.write_text(
        LexiconSnapshot(
            words={"figure": WordRoles(noun=1, verb=1)}, functions=1, accessors=0, types=1
        ).model_dump_json(),
        encoding="utf-8",
    )
    exit_code = main(["--check-new-verbs", str(head_path), str(base_path)])
    assert exit_code == 1
    assert "figure" in capsys.readouterr().out


def test_check_new_verbs_is_silent_and_zero_on_no_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    same_path = tmp_path / "same.json"
    same_path.write_text(
        LexiconSnapshot(words={"run": WordRoles(verb=1)}, functions=1, accessors=0, types=1).model_dump_json(),
        encoding="utf-8",
    )
    exit_code = main(["--check-new-verbs", str(same_path), str(same_path)])
    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_build_snapshot_merges_a_plural_noun_and_field_into_the_singular(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text(
        "from dataclasses import dataclass\n\n\n"
        "@dataclass\nclass Row:\n    row: int = 0\n\n\n"
        "@dataclass\nclass Rows:\n    rows: list = None\n",
        encoding="utf-8",
    )
    snapshot = build_snapshot(tmp_path)
    assert "rows" not in snapshot.words
    assert snapshot.words["row"].noun == 2
    assert snapshot.words["row"].field == 2
    assert snapshot.sighting("row", Role.FIELD) is not None


def test_build_snapshot_does_not_merge_a_plural_verb_conjugation(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text(
        "def _tests_shape(self) -> bool:\n    return True\n", encoding="utf-8"
    )
    snapshot = build_snapshot(tmp_path)
    assert "test" not in snapshot.words
    assert snapshot.words["tests"].verb == 1


def test_build_snapshot_does_not_merge_a_short_false_plural(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text(
        "def as_dict() -> dict:\n    return {}\n\n\nclass A(Enum):\n    pass\n", encoding="utf-8"
    )
    snapshot = build_snapshot(tmp_path)
    assert snapshot.words["a"].verb == 0
    assert snapshot.words["as"].verb == 1


def test_build_snapshot_leaves_an_unmatched_plural_alone(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text("def status_summary() -> str:\n    return ''\n", encoding="utf-8")
    snapshot = build_snapshot(tmp_path)
    assert "status" in snapshot.words


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


def test_sighting_links_to_the_blob_at_a_sha() -> None:
    head = LexiconSnapshot(
        words={"figure": WordRoles(noun=1)},
        functions=0,
        accessors=0,
        types=1,
        sightings={"figure:noun": Sighting(path="app/x/views.py", line=223, source="class PublishedFigure:")},
    )
    base = LexiconSnapshot(words={}, functions=0, accessors=0, types=0)
    body = render_markdown(head, base, SourceLinks(repo="surveit/carbonpaper", sha="abc1234"))
    assert "[app/x/views.py:223](https://github.com/surveit/carbonpaper/blob/abc1234/app/x/views.py#L223)" in body


def test_without_links_the_location_stays_plain_text() -> None:
    """A guessed URL is worse than none, so the flags are all-or-nothing."""
    head = LexiconSnapshot(
        words={"figure": WordRoles(noun=1)},
        functions=0,
        accessors=0,
        types=1,
        sightings={"figure:noun": Sighting(path="app/x/views.py", line=223, source="class PublishedFigure:")},
    )
    body = render_markdown(head, LexiconSnapshot(words={}, functions=0, accessors=0, types=0))
    assert "app/x/views.py:223" in body
    assert "https://" not in body


def test_half_the_link_arguments_is_refused() -> None:
    with pytest.raises(ValueError, match="go together"):
        main(["--markdown", "a.json", "b.json", "--repo", "surveit/carbonpaper"])
