"""The Generate-guide button: the route, the service that starts the turn, and the one
property the whole feature rests on — the agent is handed the VERSION's frozen stages,
never the working copy, which has usually moved on by the time a guide is written.
The TestClient is a context manager so its loop survives the POST and the status polls.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.compiler.review_guide as compiler_review_guide
import app.services.generation as generation
from app.core.agent.store import SessionStore
from app.core.agent.turns import TurnManager
from app.core.errors import GenerationError, ReviewGuideValidationError
from app.main import app
from app.models import find_stages_reaching_publish, parse_stage
from app.models.review_guide import ReviewGuideDraft, ReviewGuideStep
from app.services import versioning, workspace
from app.services import project as project_service
from stage_seed import add_stage

_ROWS = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_DOUBLED = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": False},
]}

_LOAD = {
    "id": "load", "description": "Load", "type": "input_data",
    "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _ROWS["columns"]},
}
_DOUBLE = {
    "id": "double", "description": "Double", "type": "python_row_function",
    "inputs": [{"id": "load", "schema": _ROWS}], "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": _ROWS["columns"]}],
        "adds": [{"name": "doubled", "type": "float", "nullable": False}],
    },
    "function": {"kind": "inline", "summary": "Doubles the amount.", "corner_cases": [],
                 "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
}
# Added to the WORKING COPY after the version is cut, never to the version itself:
# a guide naming it would be describing a workflow the version does not contain.
_TRIPLE = {
    "id": "triple", "description": "Triple", "type": "python_row_function",
    "inputs": [{"id": "load", "schema": _ROWS}], "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": _ROWS["columns"]}],
        "adds": [{"name": "doubled", "type": "float", "nullable": False}],
    },
    "function": {"kind": "inline", "summary": "Triples the amount.", "corner_cases": [],
                 "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 3}\n"},
}


def _seed_project(root: Path) -> Path:
    project_dir = root / "alpha"
    pdir = project_dir
    pdir.mkdir(parents=True, exist_ok=True)
    (project_dir / "document.md").write_text("Double the amount.", encoding="utf-8")
    add_stage(pdir, _LOAD)
    add_stage(pdir, _DOUBLE)
    return project_dir


def _add_stage_to_the_working_copy(project_dir: Path) -> None:
    add_stage(project_dir, _TRIPLE)


def _guide_of(stage_ids: list[str]) -> ReviewGuideDraft:
    return ReviewGuideDraft(steps=[ReviewGuideStep(
        title="What this does", prose="Each `amount` is doubled.", stage_ids=stage_ids,
        data_description="Every filed row, its `amount` doubled.",
    )])


def _save_guide(project_dir: Path, version_id: str, stage_ids: list[str]) -> None:
    draft = _guide_of(stage_ids)
    versioning.save_version_guide(
        project_dir,
        version_id,
        versioning.ReviewGuide(
            project=project_dir.name, version_id=version_id,
            steps=draft.steps, unnarrated=draft.unnarrated,
        ),
    )


class _FakeAuthor:
    """Faked guide-authoring Agent; `answer=None` is a turn that submits nothing."""

    task = "make a guide for this version"

    def __init__(self, answer: ReviewGuideDraft | None) -> None:
        self._submitted = answer
        self._answer: ReviewGuideDraft | None = None

    @property
    def answer(self) -> ReviewGuideDraft | None:
        return self._answer

    def build_engine(self) -> Any:
        author = self

        class _Engine:
            async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
                emit({"kind": "text", "text": "written"})
                author._answer = author._submitted
                return [{"role": "assistant", "parts": [{"type": "text", "text": "written"}]}], None

        return _Engine()


@pytest.fixture
def client(tmp_path: Path):
    workspace.set_projects_dir(tmp_path)
    with TestClient(app) as c:
        yield c


def _poll_until_inactive(client: TestClient, sid: str, *, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/project/alpha/generation-session/{sid}/status")
        assert response.status_code == 200
        data = response.json()
        if not data["active"]:
            return data
        time.sleep(0.02)
    pytest.fail("guide generation did not finish within the poll timeout")


# ── the version-scoping property ────────────────────────────────────────────


def test_the_author_is_given_the_versions_stages_not_the_working_copy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The discriminating test: the working copy gains a stage AFTER the version is
    cut."""
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )
    _add_stage_to_the_working_copy(project_dir)
    store, turns = SessionStore(), TurnManager()
    monkeypatch.setattr(compiler_review_guide, "open_session_store", lambda: store)
    monkeypatch.setattr(compiler_review_guide, "default_turn_manager", lambda: turns)
    seen: dict[str, Any] = {}

    def _spy(stages, version_id, document, *, model="sonnet"):
        seen["stage_ids"] = [stage.id for stage in stages]
        seen["task"] = compiler_review_guide.render_guide_task(stages, version_id, document)
        return _FakeAuthor(_guide_of(seen["stage_ids"]))

    monkeypatch.setattr(compiler_review_guide, "build_review_guide_author", _spy)

    async def _drive() -> None:
        sid = generation.start_review_guide_generation(
            project_dir, version_id=version.version_id, model="sonnet"
        )
        await turns._tasks[store.load(sid)["active_turn"]]

    asyncio.run(_drive())

    assert seen["stage_ids"] == ["load", "double"]
    assert "triple" not in seen["task"]
    # And the guide the turn wrote is the version's — the working copy's extra stage
    # is nowhere in it, which is also the only way save_version_guide would accept it.
    stored = versioning.find_latest_review_guide(project_dir.name, version.version_id)
    assert stored is not None
    assert stored.steps[0].stage_ids == ["load", "double"]


def test_render_guide_task_carries_the_request_the_document_and_the_stages(
    tmp_path: Path,
) -> None:
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )

    task = compiler_review_guide.render_guide_task(
        version.stages, version.version_id, "Double the amount."
    )

    assert task.startswith("make a guide for this version")
    assert "Double the amount." in task
    assert version.version_id in task
    # In execution order, and carrying the code the reviewer would be judging.
    assert task.index("Stage `load`") < task.index("Stage `double`")
    assert "row['amount'] * 2" in task


def _published_stages() -> list:
    """load -> double -> publish, plus `audit` off `load` reaching no publish stage."""
    audit = {**_TRIPLE, "id": "audit", "description": "Audit"}
    publish = {
        "id": "pub", "description": "Publish", "type": "publish",
        "inputs": [{"id": "double", "schema": _DOUBLED}],
        "publish": {"format": "csv"}, "signature": {"form": "replaces"},
        "function": {"kind": "inline",
                     "code": "def transform(df, output_dir, trace_links): return df"},
    }
    return [parse_stage(s) for s in (_LOAD, _DOUBLE, audit, publish)]


def _duty_line(task: str, stage_id: str) -> str:
    [line] = [ln for ln in task.splitlines() if ln.startswith(f"Stage `{stage_id}`")]
    return line


def test_each_stage_carries_the_requires_narration_flag() -> None:
    task = compiler_review_guide.render_guide_task(
        _published_stages(), "20260101T000000", "Double the amount."
    )

    assert "requires_narration: true" in _duty_line(task, "load")
    assert "requires_narration: true" in _duty_line(task, "double")
    assert "requires_narration: false" in _duty_line(task, "audit")
    # The publish stage narrates itself, so it carries the same flag as the leaf.
    assert "requires_narration: false" in _duty_line(task, "pub")


def test_the_flag_comes_from_the_walk_the_validator_refuses_on() -> None:
    """One function, so the flag can never say false where the validator would refuse."""
    stages = _published_stages()
    task = compiler_review_guide.render_guide_task(
        stages, "20260101T000000", "Double the amount."
    )
    reaching = find_stages_reaching_publish(stages)

    for stage in stages:
        flagged = "requires_narration: true" in _duty_line(task, stage.id)
        assert flagged is (stage.id in reaching)


def test_the_author_holds_no_tool_but_submit_answer(tmp_path: Path) -> None:
    """Structural, not prompt discipline: one tool and no built-ins, so no path exists at
    all."""
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )

    engine = compiler_review_guide.build_review_guide_author(
        version.stages, version.version_id, "Double the amount."
    ).build_engine()

    assert engine._allowed_tools == ["mcp__tools__submit_answer"]
    assert engine._builtin_tools == []


# ── the service: what it refuses, before creating a session ─────────────────


def test_start_raises_before_session_for_an_unknown_version(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project_dir = _seed_project(tmp_path)
    store = SessionStore()
    monkeypatch.setattr(compiler_review_guide, "open_session_store", lambda: store)
    before = len(store.list_sessions())

    with pytest.raises(FileNotFoundError):
        generation.start_review_guide_generation(
            project_dir, version_id="20990101-000000", model="sonnet"
        )

    assert len(store.list_sessions()) == before


def test_start_refuses_a_version_that_already_has_a_guide(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )
    _save_guide(project_dir, version.version_id, ["load", "double"])
    store = SessionStore()
    monkeypatch.setattr(compiler_review_guide, "open_session_store", lambda: store)
    before = len(store.list_sessions())

    with pytest.raises(ValueError, match="already has a review guide"):
        generation.start_review_guide_generation(
            project_dir, version_id=version.version_id, model="sonnet"
        )

    assert len(store.list_sessions()) == before


def test_start_refuses_a_project_with_no_document(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project_dir = _seed_project(tmp_path)
    (project_dir / "document.md").unlink()
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )
    store = SessionStore()
    monkeypatch.setattr(compiler_review_guide, "open_session_store", lambda: store)
    before = len(store.list_sessions())

    with pytest.raises(ValueError, match="no document"):
        generation.start_review_guide_generation(
            project_dir, version_id=version.version_id, model="sonnet"
        )

    assert len(store.list_sessions()) == before


# ── the completion hook ─────────────────────────────────────────────────────


def test_finish_stores_the_guide_on_the_version(tmp_path: Path) -> None:
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )

    generation._finish_review_guide(
        project_dir, version.version_id, _guide_of(["load", "double"])
    )

    stored = versioning.find_latest_review_guide(project_dir.name, version.version_id)
    assert stored is not None
    assert stored.steps[0].stage_ids == ["load", "double"]


def test_finish_with_no_guide_raises_and_writes_nothing(tmp_path: Path) -> None:
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )

    with pytest.raises(GenerationError, match="did not submit a guide"):
        generation._finish_review_guide(project_dir, version.version_id, None)

    assert versioning.find_latest_review_guide(project_dir.name, version.version_id) is None


def test_finish_refuses_a_guide_that_misses_a_stage(tmp_path: Path) -> None:
    """A guide accounting for only some stages leaves the version with none at all."""
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )

    with pytest.raises(ReviewGuideValidationError, match="double"):
        generation._finish_review_guide(
            project_dir, version.version_id, _guide_of(["load"])
        )

    assert versioning.find_latest_review_guide(project_dir.name, version.version_id) is None


# ── the route ───────────────────────────────────────────────────────────────


def test_post_generates_the_guide_and_stores_it_on_the_version(
    client: TestClient, tmp_path: Path, monkeypatch: Any
) -> None:
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )
    monkeypatch.setattr(compiler_review_guide, "default_turn_manager", lambda: TurnManager())
    monkeypatch.setattr(
        compiler_review_guide, "build_review_guide_author",
        lambda *a, **k: _FakeAuthor(_guide_of(["load", "double"])),
    )

    response = client.post(f"/project/alpha/workflow/version/{version.version_id}/guide")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    status = _poll_until_inactive(client, response.json()["session"])
    assert status["error"] is None
    assert versioning.find_latest_review_guide(project_dir.name, version.version_id) is not None


def test_post_reports_a_turn_that_submitted_nothing(
    client: TestClient, tmp_path: Path, monkeypatch: Any
) -> None:
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )
    monkeypatch.setattr(compiler_review_guide, "default_turn_manager", lambda: TurnManager())
    monkeypatch.setattr(
        compiler_review_guide, "build_review_guide_author",
        lambda *a, **k: _FakeAuthor(None),
    )

    response = client.post(f"/project/alpha/workflow/version/{version.version_id}/guide")

    status = _poll_until_inactive(client, response.json()["session"])
    assert status["error"] is not None
    assert "did not submit a guide" in status["error"]
    assert versioning.find_latest_review_guide(project_dir.name, version.version_id) is None


def test_post_for_an_unknown_version_is_404(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path)

    response = client.post("/project/alpha/workflow/version/20990101-000000/guide")

    assert response.status_code == 404


def test_post_for_an_unknown_project_is_404(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/project/nosuch/workflow/version/20990101-000000/guide")

    assert response.status_code == 404


def test_post_for_a_version_that_already_has_a_guide_is_400(
    client: TestClient, tmp_path: Path
) -> None:
    project_dir = _seed_project(tmp_path)
    version = project_service.save_working_copy_as_version(
        project_dir, message="v1", reviewer="local"
    )
    _save_guide(project_dir, version.version_id, ["load", "double"])

    response = client.post(f"/project/alpha/workflow/version/{version.version_id}/guide")

    assert response.status_code == 400
    assert "already has a review guide" in response.json()["detail"]
