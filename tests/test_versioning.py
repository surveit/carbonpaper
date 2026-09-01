"""Project scoping is by `tmp_path.name`, isolated per test by the autouse in-memory
store (conftest.fresh_store).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import pydantic

from app.core.errors import NoVersionToRunError, ReviewGuideValidationError
from app.models import AbstractStage
from app.models.review_guide import ReviewGuideStep
from app.core.persistence import get_store
from app.services.loader import WorkflowLoadError
from app.services.project import save_working_copy_as_version
from app.services.versioning import create_version_from_stages, list_versions, load_version, find_latest_review_guide, find_latest_version_id, load_version_stages, resolve_version_id, save_version_guide
from app.models.records.review_guide import ReviewGuide
from app.models.records.workflow_version import WorkflowVersion
from stage_seed import add_stage

# AbstractStage._schemas_declared wants every non-report stage to say what it outputs.
_ROWS_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": False}]}

_LOAD_STAGE = {
    "id": "load", "description": "Load", "type": "input_data",
    "connector": {"kind": "file"},
    "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
}


def _seed(project_dir: Path, stage: dict = _LOAD_STAGE) -> None:
    """A path-free file connector, so no data file need exist: nothing here executes."""
    compiled = project_dir
    compiled.mkdir(parents=True, exist_ok=True)
    add_stage(compiled, stage)


# ── save_working_copy_as_version ─────────────────────────────────────────────────

def test_create_version_returns_meta_and_round_trips(tmp_path):
    _seed(tmp_path)
    meta = save_working_copy_as_version(tmp_path.name, message="first cut")

    assert meta.message == "first cut"
    assert meta.parent_version is None

    [listed] = list_versions(tmp_path.name)
    assert listed == meta
    assert load_version(tmp_path.name, meta.version_id) == meta

    [stage] = load_version_stages(tmp_path.name, meta.version_id)
    assert isinstance(stage, AbstractStage)
    assert stage.id == "load"


def test_create_version_records_parent(tmp_path):
    _seed(tmp_path)

    first = save_working_copy_as_version(tmp_path.name, message="v1")
    second = save_working_copy_as_version(tmp_path.name, message="v2",
                                      parent_version=first.version_id)
    assert second.version_id != first.version_id
    assert second.parent_version == first.version_id


def test_create_version_no_compiled_dir_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        save_working_copy_as_version(tmp_path.name, message="x")
    assert list_versions(tmp_path.name) == []


def test_create_version_invalid_workflow_raises_and_writes_nothing(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bad = {"id": "load", "description": "Load", "type": "input_data",
           "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}}}  # relative path
    add_stage(tmp_path, bad)

    with pytest.raises(WorkflowLoadError) as exc:
        save_working_copy_as_version(tmp_path.name, message="x")
    assert any("params.path" in i for i in exc.value.issues)
    assert list_versions(tmp_path.name) == []


def test_create_version_twice_within_a_second_keeps_both(tmp_path):
    _seed(tmp_path)

    save_working_copy_as_version(tmp_path.name, message="first")
    save_working_copy_as_version(tmp_path.name, message="second")

    assert [v.message for v in list_versions(tmp_path.name)] == ["second", "first"]


def test_versions_are_scoped_per_project(tmp_path):
    proj_a, proj_b = tmp_path / "alpha", tmp_path / "beta"
    _seed(proj_a)
    _seed(proj_b)
    meta_a = save_working_copy_as_version(proj_a.name, message="a")
    meta_b = save_working_copy_as_version(proj_b.name, message="b")
    assert [v.version_id for v in list_versions(proj_a.name)] == [meta_a.version_id]
    assert [v.version_id for v in list_versions(proj_b.name)] == [meta_b.version_id]


# ── list_versions ────────────────────────────────────────────────────────────

def test_list_versions_empty_when_none_created(tmp_path):
    assert list_versions(tmp_path.name) == []


def test_list_versions_newest_first(tmp_path):
    for vid in ("20260101T000000", "20260201T000000", "20260115T000000"):
        WorkflowVersion(id=f"{tmp_path.name}/{vid}", version_id=vid, created_at=vid,
                message="m").save()
    assert [v.version_id for v in list_versions(tmp_path.name)] == [
        "20260201T000000", "20260115T000000", "20260101T000000"]


def test_list_versions_errors_on_a_corrupt_document(tmp_path):
    _seed(tmp_path)
    save_working_copy_as_version(tmp_path.name, message="good")
    get_store().write("workflow_version", f"{tmp_path.name}/20260101T000000", {"bogus": "data"})
    with pytest.raises(WorkflowLoadError, match="20260101T000000"):
        list_versions(tmp_path.name)


# ── load_version / load_version_stages ─────────────────────────────────────

def test_load_version_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_version(tmp_path.name, "nope")


def test_load_version_stages_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_version_stages(tmp_path.name, "nope")


def test_a_stored_version_still_carrying_the_retired_publish_keys_is_refused(tmp_path):
    """What alembic 0020 exists to prevent: PersistedModel forbids extra keys."""
    vid = "20260101T000000"
    data = {
        "id": f"{tmp_path.name}/{vid}", "version_id": vid,
        "created_at": "2026-01-01T00:00:00", "parent_version": None,
        "message": "legacy", "reviewer": "human", "published": False,
        "stages": [], "schemas": [],
    }
    get_store().write("workflow_version", f"{tmp_path.name}/{vid}", data)
    with pytest.raises(WorkflowLoadError):
        load_version(tmp_path.name, vid)


def test_a_stored_queue_stage_written_before_queue_sort_still_loads(tmp_path):
    """A queue block with no `sort` key is every such document written before the field."""
    vid = "20260101T000000"
    reviewed = [
        {"name": "human_score", "type": "int", "nullable": True},
        {"name": "decision", "type": "str", "nullable": True},
        {"name": "reviewer_id", "type": "str", "nullable": True},
        {"name": "reviewed_at", "type": "str", "nullable": True},
    ]
    scored = [
        {"name": "doc_id", "type": "str", "nullable": False},
        {"name": "score", "type": "int", "nullable": True},
    ]
    stage = {
        "id": "review", "description": "Review", "type": "human_review_queue",
        "inputs": [{"id": "load"}],
        "signature": {"form": "extends", "adds": reviewed,
                      "reads": [{"input": "load", "columns": scored}]},
        "queue": {
            "reviewed_columns": {"score": "human_score"}, "verdict_column": "decision",
            "reviewer_column": "reviewer_id", "reviewed_at_column": "reviewed_at",
        },
    }
    get_store().write("workflow_version", f"{tmp_path.name}/{vid}", {
        "id": f"{tmp_path.name}/{vid}", "version_id": vid,
        "created_at": "2026-01-01T00:00:00", "parent_version": None,
        "message": "legacy", "stages": [stage], "schemas": [],
    })

    [loaded] = load_version_stages(tmp_path.name, vid)
    assert loaded.queue.sort == []


# ── find_latest_version_id / resolve_version_id ──────────────────────────────

def _store_version(project_dir: Path, vid: str) -> str:
    WorkflowVersion(id=f"{project_dir.name}/{vid}", version_id=vid, created_at=vid,
                    message="m").save()
    return vid


def test_find_latest_version_id_is_none_when_the_project_stores_none(tmp_path):
    assert find_latest_version_id(tmp_path.name) is None


def test_find_latest_version_id_returns_the_newest_whatever_its_published_state(tmp_path):
    _store_version(tmp_path, "20260101T000000")
    newest = _store_version(tmp_path, "20260201T000000")
    assert find_latest_version_id(tmp_path.name) == newest


def test_resolve_version_id_defaults_to_the_newest_stored_version(tmp_path):
    _store_version(tmp_path, "20260101T000000")
    newest = _store_version(tmp_path, "20260201T000000")
    assert resolve_version_id(tmp_path.name, None) == newest


def test_resolve_version_id_returns_a_named_unpublished_version(tmp_path):
    vid = _store_version(tmp_path, "20260101T000000")
    assert resolve_version_id(tmp_path.name, vid) == vid


def test_resolve_version_id_returns_a_named_published_version(tmp_path):
    vid = _store_version(tmp_path, "20260101T000000")
    assert resolve_version_id(tmp_path.name, vid) == vid


def test_resolve_version_id_raises_file_not_found_for_an_unknown_id(tmp_path):
    _store_version(tmp_path, "20260101T000000")
    with pytest.raises(FileNotFoundError):
        resolve_version_id(tmp_path.name, "nope")


def test_resolve_version_id_raises_when_the_project_stores_no_version_at_all(tmp_path):
    with pytest.raises(NoVersionToRunError, match=tmp_path.name):
        resolve_version_id(tmp_path.name, None)


# ── create_version_from_stages: the single write chokepoint ─────────────────

def test_create_version_from_stages_valid_is_loadable(tmp_path):
    meta = create_version_from_stages(tmp_path.name, [_LOAD_STAGE], message="from stages",
        parent_version="prior-id",
    )
    assert meta.parent_version == "prior-id"

    [stage] = load_version_stages(tmp_path.name, meta.version_id)
    assert isinstance(stage, AbstractStage)
    assert stage.id == "load"


def test_create_version_from_stages_invalid_raises_and_writes_nothing(tmp_path):
    dangling_input = {
        "id": "consume", "description": "Consume", "type": "python_frame_function",
        "inputs": [{"id": "no-such-stage"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "no-such-stage", "columns": _ROWS_SCHEMA["columns"]}],
            "produces": _ROWS_SCHEMA["columns"],
        },
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
    }
    with pytest.raises(pydantic.ValidationError):
        create_version_from_stages(tmp_path.name, [dangling_input], message="bad",
        )
    assert list_versions(tmp_path.name) == []


# ── the version's review guide ───────────────────────────────────────────────

_TALLY_STAGE = {
    "id": "tally", "description": "Tally", "type": "input_data",
    "connector": {"kind": "file"},
    "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
}


def _two_stage_version(project_dir: Path) -> str:
    return create_version_from_stages(project_dir.name, [_LOAD_STAGE, _TALLY_STAGE], message="two",
    ).version_id


def _guide(
    project_dir: Path, version_id: str, step_ids: list[str], unnarrated: list[str],
) -> ReviewGuide:
    return ReviewGuide(
        project=project_dir.name,
        version_id=version_id,
        steps=[ReviewGuideStep(title="Load the docs", prose="Reads `doc_id`.",
                               stage_ids=step_ids,
                               data_description="Every document the desk filed.")],
        unnarrated=unnarrated,
    )


def test_a_guide_is_found_by_its_backpointers(tmp_path):
    guide = ReviewGuide(project="demo", version_id="20260101T000000", steps=[])

    assert guide.id and guide.id != "demo/20260101T000000"
    assert "id" not in ReviewGuide.model_json_schema()["required"]


def _recording_write(real_write, collections: list[str]):
    def write(collection, id, data, schema_version=1):
        collections.append(collection)
        real_write(collection, id, data, schema_version)
    return write


def test_save_version_guide_round_trips_as_its_own_document(tmp_path):
    vid = _two_stage_version(tmp_path)
    saved = save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load"], ["tally"]))

    stored = get_store().read("review_guide", saved.id)
    assert (stored["project"], stored["version_id"]) == (tmp_path.name, vid)

    reloaded = find_latest_review_guide(tmp_path.name, vid)
    assert reloaded is not None
    assert reloaded.unnarrated == ["tally"]
    assert reloaded.collect_step_stage_ids() == ["load"]
    assert [(s.title, s.prose) for s in reloaded.steps] == [("Load the docs", "Reads `doc_id`.")]


def test_saving_a_guide_does_not_rewrite_the_version_document(tmp_path, monkeypatch):
    vid = _two_stage_version(tmp_path)
    doc_id = f"{tmp_path.name}/{vid}"
    store = get_store()
    before = store.read("workflow_version", doc_id)
    written: list[str] = []
    monkeypatch.setattr(store, "write", _recording_write(store.write, written))

    save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load"], ["tally"]))

    # The recorded writes are the load-bearing assertion — comparing the document
    # alone would pass a re-save that happened to produce identical bytes.
    assert written == ["review_guide"]
    assert store.read("workflow_version", doc_id) == before


def test_the_newest_guide_is_the_one_a_reader_gets(tmp_path):
    vid = _two_stage_version(tmp_path)
    save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load"], ["tally"]))
    save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load", "tally"], []))

    guide = find_latest_review_guide(tmp_path.name, vid)
    assert guide is not None
    assert guide.unnarrated == []
    assert guide.collect_step_stage_ids() == ["load", "tally"]


def test_save_version_guide_rejects_an_unknown_id_in_a_step(tmp_path):
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError, match="ghost"):
        save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load", "ghost"], ["tally"]))
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_rejects_an_unknown_id_in_unnarrated(tmp_path):
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError, match="ghost"):
        save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load"], ["tally", "ghost"]))
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_rejects_a_stage_accounted_for_nowhere(tmp_path):
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError, match="tally"):
        save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load"], []))
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_rejects_a_stage_both_narrated_and_unnarrated(tmp_path):
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError, match="load"):
        save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load", "tally"], ["load"]))
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_rejects_a_stage_narrated_by_two_steps(tmp_path):
    vid = _two_stage_version(tmp_path)
    two_steps = ReviewGuide(
        project=tmp_path.name,
        version_id=vid,
        steps=[
            ReviewGuideStep(title="First", prose="a", stage_ids=["load"],
                            data_description="The documents, as filed."),
            ReviewGuideStep(title="Second", prose="b", stage_ids=["load", "tally"],
                            data_description="The documents, tallied."),
        ],
    )
    with pytest.raises(ReviewGuideValidationError, match="more than once"):
        save_version_guide(tmp_path.name, vid, two_steps)
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_reports_every_offending_id_at_once(tmp_path):
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError) as exc:
        save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["ghost"], []))
    message = str(exc.value)
    assert "ghost" in message and "tally" in message and "load" in message


# ── `unnarrated` may hide only a stage no report stage reads ────────────────

def _published_version(project_dir: Path, report_reads: str) -> str:
    stages: list[dict] = [
        _LOAD_STAGE,
        {"id": "mid", "description": "Middle", "type": "python_frame_function",
         "inputs": [{"id": "load"}],
         "function": {"kind": "inline", "code": "def transform(df): return df"},
         "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]}},
        {"id": "checked", "description": "Assert something", "type": "python_frame_function",
         "inputs": [{"id": "mid"}],
         "function": {"kind": "inline", "code": "def transform(df): return df"},
         "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]}},
        {"id": "pub", "description": "Publish", "type": "report",
         "inputs": [{"id": report_reads}],
         "report": {"format": "csv"},
         "function": {"kind": "inline",
                      "code": "def transform(df, output_dir, citation_provider): return df"},
         "signature": {"form": "replaces"}},
    ]
    return create_version_from_stages(project_dir.name, stages, message="published",
    ).version_id


def test_save_version_guide_refuses_an_unnarrated_stage_that_feeds_report(tmp_path):
    vid = _published_version(tmp_path, report_reads="mid")
    guide = _guide(tmp_path, vid, ["load", "checked", "pub"], ["mid"])

    with pytest.raises(ReviewGuideValidationError, match="mid"):
        save_version_guide(tmp_path.name, vid, guide)
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_the_refusal_reaches_through_intermediate_stages(tmp_path):
    vid = _published_version(tmp_path, report_reads="mid")
    guide = _guide(tmp_path, vid, ["mid", "checked", "pub"], ["load"])

    with pytest.raises(ReviewGuideValidationError) as exc:
        save_version_guide(tmp_path.name, vid, guide)
    assert "'load'" in str(exc.value)


def test_a_report_stage_may_be_unnarrated_because_it_narrates_itself(tmp_path):
    vid = _published_version(tmp_path, report_reads="mid")
    guide = _guide(tmp_path, vid, ["load", "mid", "checked"], ["pub"])

    saved = save_version_guide(tmp_path.name, vid, guide)

    assert saved.unnarrated == ["pub"]


def test_the_stage_feeding_the_report_is_refused_where_the_report_itself_is_allowed(tmp_path):
    vid = _published_version(tmp_path, report_reads="mid")

    save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load", "mid", "checked"], ["pub"]))
    with pytest.raises(ReviewGuideValidationError, match="mid"):
        save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load", "checked", "pub"], ["mid"]))


def test_a_stage_reaching_no_report_stage_may_still_be_unnarrated(tmp_path):
    vid = _published_version(tmp_path, report_reads="mid")
    guide = _guide(tmp_path, vid, ["load", "mid", "pub"], ["checked"])

    saved = save_version_guide(tmp_path.name, vid, guide)

    assert find_latest_review_guide(tmp_path.name, vid).id == saved.id
    assert saved.unnarrated == ["checked"]


def test_the_refusal_names_every_hidden_stage_and_says_why(tmp_path):
    vid = _published_version(tmp_path, report_reads="mid")
    guide = _guide(tmp_path, vid, ["checked", "pub"], ["load", "mid"])

    with pytest.raises(ReviewGuideValidationError) as exc:
        save_version_guide(tmp_path.name, vid, guide)
    message = str(exc.value)
    assert "'load'" in message and "'mid'" in message
    assert "reaches a report stage" in message and "Narrate each in a section" in message


# ── the data sentence: required to WRITE a guide, optional in the store ──────

def _guide_with_data_descriptions(
    project_dir: Path, version_id: str, *sentences: str | None
) -> ReviewGuide:
    return ReviewGuide(
        project=project_dir.name,
        version_id=version_id,
        steps=[
            ReviewGuideStep(title=f"Section {n}", prose="p", stage_ids=[stage_id],
                            data_description=sentence)
            for n, (stage_id, sentence) in enumerate(zip(["load", "tally"], sentences), 1)
        ],
    )


def test_save_version_guide_refuses_a_section_with_no_data_sentence(tmp_path):
    vid = _two_stage_version(tmp_path)
    guide = _guide_with_data_descriptions(tmp_path, vid, "The documents filed.", None)

    with pytest.raises(ReviewGuideValidationError, match="data_description"):
        save_version_guide(tmp_path.name, vid, guide)
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_the_refusal_names_which_sections_are_missing_the_sentence(tmp_path):
    vid = _two_stage_version(tmp_path)
    guide = _guide_with_data_descriptions(tmp_path, vid, None, "   ")

    with pytest.raises(ReviewGuideValidationError) as exc:
        save_version_guide(tmp_path.name, vid, guide)
    message = str(exc.value)
    assert "1 ('Section 1')" in message and "2 ('Section 2')" in message


def test_a_blank_data_sentence_is_refused_as_absent(tmp_path):
    vid = _two_stage_version(tmp_path)
    guide = _guide_with_data_descriptions(tmp_path, vid, "The documents filed.", "  \n ")

    with pytest.raises(ReviewGuideValidationError, match=r"2 \('Section 2'\)"):
        save_version_guide(tmp_path.name, vid, guide)


def test_a_guide_stored_before_the_data_sentence_existed_still_loads(tmp_path):
    vid = _two_stage_version(tmp_path)
    saved = save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load"], ["tally"]))
    payload = get_store().read("review_guide", saved.id)
    # The record every guide written before the field looks like. It goes back through
    # PersistedModel.load's extra="forbid" model_validate, which grants no leniency —
    # this is the whole reason ReviewGuideStep.data_description stays optional.
    for step in payload["steps"]:
        del step["data_description"]
    get_store().write("review_guide", saved.id, payload)

    assert [s.data_description for s in ReviewGuide.load(saved.id).steps] == [None]


def test_save_version_guide_unknown_version_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        save_version_guide(tmp_path.name, "nope", _guide(tmp_path, "nope", [], []))


def test_save_version_guide_rejects_a_guide_addressed_to_another_version(tmp_path):
    vid = _two_stage_version(tmp_path)
    other = _guide(tmp_path, "20990101T000000", ["load"], ["tally"])
    with pytest.raises(ValueError, match="20990101T000000"):
        save_version_guide(tmp_path.name, vid, other)
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_find_latest_review_guide_is_none_when_no_guide_was_saved(tmp_path):
    vid = _two_stage_version(tmp_path)
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_writing_a_second_guide_appends_and_the_newest_one_wins(tmp_path):
    vid = _two_stage_version(tmp_path)
    first = save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load"], ["tally"]))
    second = save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["tally"], ["load"]))

    assert first.id != second.id
    assert {g.id for g in ReviewGuide.list()} == {first.id, second.id}
    assert find_latest_review_guide(tmp_path.name, vid).id == second.id


def test_a_guide_for_another_version_is_not_returned(tmp_path):
    vid = _two_stage_version(tmp_path)
    save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load"], ["tally"]))

    assert find_latest_review_guide(tmp_path.name, "20200101T000000") is None
    assert find_latest_review_guide("another_project", vid) is None


def test_a_version_document_carries_no_guide_key(tmp_path):
    vid = _two_stage_version(tmp_path)
    save_version_guide(tmp_path.name, vid, _guide(tmp_path, vid, ["load"], ["tally"]))
    assert "guide" not in get_store().read("workflow_version", f"{tmp_path.name}/{vid}")


def test_a_version_document_with_an_embedded_guide_fails_loudly(tmp_path):
    vid = "20260101T000000"
    data = {
        "id": f"{tmp_path.name}/{vid}", "version_id": vid,
        "created_at": "2026-01-01T00:00:00", "parent_version": None,
        "message": "legacy", "stages": [], "schemas": [], "guide": {"steps": [], "unnarrated": []},
    }
    get_store().write("workflow_version", f"{tmp_path.name}/{vid}", data)
    with pytest.raises(WorkflowLoadError, match="guide"):
        load_version(tmp_path.name, vid)
