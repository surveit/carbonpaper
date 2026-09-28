"""A row a model decided on a project run is on the judgment ledger, and its page shows the call."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import app.services.workspace as workspace
from app.core.agent.usage import LlmUsage
from app.core.judgments import Judgment, JudgmentDraft, JudgmentKind
from app.core.stage_cache import ReadOnlyStageCache, StageCache, StageCacheEntry
from app.core.timestamp_ids import now_iso
from app.main import app
from app.models import Stage, parse_stage
from app.models.stage import StageType
from app.models.stages.llm_transform import LLMConfig
from app.runtime import llm as runtime_llm
from app.runtime import options
from app.runtime.context import RunIdentity
from app.runtime.errors import JudgmentUnrecorded
from app.runtime.run_log import JUDGMENT_ID, ROW_OK, SOURCE_CACHED, SOURCE_COMPUTED, read_events_since
from app.runtime.runner import execute_run
from app.runtime.stage_output import StageOutput
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from app.runtime.stages.execution import ROW_JUDGMENT_KEY
from app.web import judgment_view
from conftest import as_inputs, make_run_context, pinned_stages, place_stage, rows_of
from stage_seed import add_stage, save_version

PROJECT = "judgment_ledger"
_X = [{"name": "x", "type": "int", "nullable": True}]


def _judge_spec(*, batch_size: int = 1, cache: bool = True) -> dict[str, Any]:
    return {
        "id": "judge", "description": "Judge each row", "type": "llm_transform",
        "inputs": [{"id": "load"}], "cache": cache,
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _X}],
            "adds": [{"name": "verdict", "type": "str", "nullable": True}],
        },
        "llm": {"prompt_instructions": "judge it", "prompt_data_template": "Rate: {x}",
                "model": "claude-haiku-4-5", "batch_size": batch_size},
    }


def _draft(reply: dict[str, Any], task: str = "Rate: 1") -> JudgmentDraft:
    return JudgmentDraft(
        system_prompt="told", task=task, model="claude-haiku-4-5", reply=reply,
        usage=LlmUsage(input_tokens=12, output_tokens=3, cost_usd=0.01, calls=1,
                       model="claude-haiku-4-5"),
        decided_at=now_iso(),
    )


def _ctx(run_id: str = "r1", cache: ReadOnlyStageCache | None = None):
    return make_run_context(
        identity=RunIdentity(project=PROJECT, run_id=run_id),
        stage_cache=StageCache() if cache is None else cache,
    )


def _execute(stage: Stage, values: list[int], ctx) -> StageOutput:
    placed = place_stage(stage, load={"columns": _X})
    out = HANDLERS[StageType.llm_transform].execute(
        placed, as_inputs({"load": pd.DataFrame({"x": values})}), ctx)
    assert out is not None
    return out


def _judgments() -> list[Judgment]:
    return Judgment.find(project_id=PROJECT)


def _entries(stage: Stage) -> list[StageCacheEntry]:
    return ReadOnlyStageCache().find_entries(
        PROJECT, stage.id, stage.compute_definition_fingerprint())


def _answer_per_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lt, "call_llm", lambda stage_id, llm, row, **kw: _draft(
        {"verdict": f"v{row['x']}"}, task=f"Rate: {row['x']}"))


# ── call_llm hands back what the model was told, asked and answered ─────────


class _Verdict(BaseModel):
    verdict: str


class _ScriptedAgent:
    def __init__(self, *, target_schema: type[BaseModel], task: str, **_kwargs: Any) -> None:
        self._schema, self._task = target_schema, task
        self.last_usage = LlmUsage(input_tokens=12, output_tokens=3, cost_usd=0.01, calls=1)

    async def run(self, emit: Any = None) -> BaseModel:
        return self._schema.model_validate({"verdict": f"read {self._task}"})


@pytest.fixture()
def scripted_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(options, "agent_available", lambda: True)
    monkeypatch.setattr(runtime_llm, "Agent", _ScriptedAgent)


def test_call_llm_returns_the_composed_prompt_the_task_the_model_and_the_reply(scripted_agent):
    config = LLMConfig(prompt_instructions="judge it", prompt_data_template="Rate: {x}",
                       model="claude-sonnet-5")
    usages: list[LlmUsage] = []

    draft = runtime_llm.call_llm("judge", config, {"x": 7}, reply_model=_Verdict,
                                 usage_out=usages)

    assert draft.system_prompt == runtime_llm.SYSTEM_PROMPT + "\n\njudge it"
    assert draft.task == "Rate: 7"
    assert draft.model == "claude-sonnet-5"
    assert draft.reply == {"verdict": "read Rate: 7"}
    assert draft.usage == LlmUsage(input_tokens=12, output_tokens=3, cost_usd=0.01, calls=1,
                                   model="claude-sonnet-5")
    assert usages == [draft.usage]


# ── the row driver records ──────────────────────────────────────────────────


def test_a_computed_row_is_on_the_ledger_and_its_cache_entry_points_at_it(monkeypatch):
    _answer_per_row(monkeypatch)
    stage = parse_stage(_judge_spec())

    _execute(stage, [1], _ctx("r1"))

    [judgment] = _judgments()
    [entry] = _entries(stage)
    assert entry.judgment_id == judgment.id
    assert judgment.run_id == "r1" and judgment.stage_id == "judge"
    assert judgment.kind == JudgmentKind.MODEL
    assert judgment.input_fingerprint == entry.input_fingerprint
    assert judgment.frozen_input == {"x": 1}
    assert (judgment.system_prompt, judgment.task, judgment.model) == (
        "told", "Rate: 1", "claude-haiku-4-5")
    assert judgment.reply == {"verdict": "v1"}
    assert judgment.usage.calls == 1


def test_the_judgment_column_reaches_neither_the_output_nor_the_cache(monkeypatch):
    _answer_per_row(monkeypatch)
    stage = parse_stage(_judge_spec())

    out = _execute(stage, [1], _ctx())

    assert ROW_JUDGMENT_KEY not in rows_of(out).columns
    [entry] = _entries(stage)
    assert entry.output_row is not None and ROW_JUDGMENT_KEY not in entry.output_row


def test_a_stage_that_never_caches_still_records_its_judgments(monkeypatch):
    _answer_per_row(monkeypatch)
    stage = parse_stage(_judge_spec(cache=False))

    _execute(stage, [1, 2], _ctx())

    assert sorted(j.reply["verdict"] for j in _judgments()) == ["v1", "v2"]
    assert _entries(stage) == []


def test_a_run_that_may_not_write_across_runs_records_no_judgment(monkeypatch):
    _answer_per_row(monkeypatch)

    _execute(parse_stage(_judge_spec()), [1], _ctx(cache=ReadOnlyStageCache()))

    assert _judgments() == []


def test_each_row_of_a_batched_call_is_judged_by_that_call(monkeypatch):
    monkeypatch.setattr(lt, "call_llm_batch", lambda *a, **k: _draft(
        {"results": [{"row_number": 0, "verdict": "a"}, {"row_number": 1, "verdict": "b"}]},
        task=k["task"]))

    _execute(parse_stage(_judge_spec(batch_size=2)), [1, 2], _ctx())

    judgments = sorted(_judgments(), key=lambda j: j.frozen_input["x"])
    assert [j.frozen_input for j in judgments] == [{"x": 1}, {"x": 2}]
    assert judgments[0].task == judgments[1].task and "### item 1" in judgments[0].task
    page = TestClient(app).get(f"/project/{PROJECT}/judgments/{judgments[0].id}").text
    assert "one call covering 2 rows, this one among them" in page


# ── G2: a row a model decided owes a judgment ───────────────────────────────


def _answer_with_no_judgment(monkeypatch: pytest.MonkeyPatch) -> None:
    def make_mapper(workflow_stage):
        def map_group(indices, rows):
            return [{**row, "verdict": "v"} for row in rows]
        return map_group

    monkeypatch.setattr(lt, "build_llm_batch_mapper", make_mapper)


def test_a_row_with_no_judgment_stops_the_stage_on_a_project_run(monkeypatch):
    _answer_with_no_judgment(monkeypatch)

    with pytest.raises(JudgmentUnrecorded, match="row 0"):
        _execute(parse_stage(_judge_spec(batch_size=2)), [1, 2], _ctx())


def test_outside_a_run_no_judgment_is_owed(monkeypatch):
    _answer_with_no_judgment(monkeypatch)

    out = _execute(parse_stage(_judge_spec(batch_size=2)), [1, 2], make_run_context())

    assert list(rows_of(out)["verdict"]) == ["v", "v"]


def test_a_failed_row_owes_no_judgment(monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(lt, "call_llm", fail)

    out = _execute(parse_stage(_judge_spec()), [1], _ctx())

    assert out.contribution.row_errors == [{"row": 0, "message": "model down"}]
    assert _judgments() == []


# ── a scripted run end to end: the log, the replay, the pages ───────────────


@pytest.fixture()
def project(tmp_path: Path, scripted_agent) -> Path:
    pdir = tmp_path / PROJECT
    pdir.mkdir(parents=True, exist_ok=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"x": [1, 2]}).to_csv(data, index=False)
    add_stage(pdir, {
        "id": "load", "description": "Load rows", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(data), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _X},
    })
    add_stage(pdir, _judge_spec())
    workspace.set_projects_dir(tmp_path)
    save_version(pdir.name, message="v1")
    return pdir


def _run(project_dir: Path) -> str:
    return str(execute_run(
        project_dir / "runs", project_dir.name, *pinned_stages(project_dir))["run_id"])


def _row_oks(run_id: str) -> list[dict[str, Any]]:
    return [e for e in read_events_since(PROJECT, run_id, 0)
            if e.get("stage") == "judge" and e["kind"] == ROW_OK]


def test_the_log_names_each_judgment_when_computed_and_when_replayed(project):
    first = _run(project)
    replay = _run(project)

    computed = {e["row"]: e[JUDGMENT_ID] for e in _row_oks(first)}
    assert {e["source"] for e in _row_oks(first)} == {SOURCE_COMPUTED}
    assert set(computed.values()) == {j.id for j in _judgments()}
    assert {e["row"]: (e["source"], e[JUDGMENT_ID]) for e in _row_oks(replay)} == {
        row: (SOURCE_CACHED, judgment_id) for row, judgment_id in computed.items()}


def test_the_judgment_page_shows_the_call_and_links_its_run_stage_and_row(project):
    run_id = _run(project)
    [judgment] = [j for j in _judgments() if j.frozen_input == {"x": 2}]

    response = TestClient(app).get(f"/project/{PROJECT}/judgments/{judgment.id}")

    assert response.status_code == 200, response.text
    html = response.text
    assert "judge it" in html and "Rate: 2" in html and "read Rate: 2" in html
    assert "<code>claude-haiku-4-5</code>" in html
    assert f'href="/project/{PROJECT}/runs/{run_id}"' in html
    assert f'href="/project/{PROJECT}/runs/{run_id}#judge"' in html
    assert f'href="/project/{PROJECT}/runs/{run_id}/stage/judge/row/1/trace/view"' in html


def test_the_judgment_page_is_not_served_under_another_project(project):
    _run(project)
    [judgment, _] = _judgments()
    client = TestClient(app)

    assert client.get(f"/project/elsewhere/judgments/{judgment.id}").status_code == 404
    assert client.get(f"/project/{PROJECT}/judgments/no-such-judgment").status_code == 404


def test_the_rows_page_links_each_row_to_its_judgment(project):
    run_id = _run(project)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/stage/judge/rows").text

    for judgment in _judgments():
        assert f'href="/project/{PROJECT}/judgments/{judgment.id}"' in html


def test_the_rows_page_links_no_judgment_this_project_does_not_store(project):
    run_id = _run(project)
    kept, dropped = _judgments()
    Judgment.delete(dropped.id)

    html = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/stage/judge/rows").text

    assert f"/judgments/{kept.id}" in html and f"/judgments/{dropped.id}" not in html


def test_the_rows_page_of_a_stage_no_model_answered_reads_no_log(project, monkeypatch):
    run_id = _run(project)

    def refuse(*a, **k):
        raise AssertionError("read the run log for a stage that owes no judgment")

    monkeypatch.setattr(judgment_view, "read_events_since", refuse)
    response = TestClient(app).get(f"/project/{PROJECT}/runs/{run_id}/stage/load/rows")

    assert response.status_code == 200, response.text
