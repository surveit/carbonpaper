from __future__ import annotations

import pandas as pd

import app.runtime.stages.llm_transform as lt
from app.core.agent.usage import LlmUsage
from app.models import parse_stage, Stage
from app.models.stage import StageType
from app.runtime.stages import HANDLERS
from conftest import contribution_of, make_run_context, pinned_stages, place_stage


def test_summed_adds_fields_and_counts_calls():
    a = LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)
    b = LlmUsage(input_tokens=7, output_tokens=3, cost_usd=0.002, calls=1)
    assert LlmUsage.summed([a, b]) == LlmUsage(
        input_tokens=17, output_tokens=7, cost_usd=0.003, calls=2)


def test_summed_of_nothing_is_the_zero_instance():
    assert LlmUsage.summed([]) == LlmUsage()


def _llm_stage() -> Stage:
    return parse_stage({
        "id": "classify", "description": "Classify", "type": "llm_transform",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "text", "type": "str", "nullable": True}],
                },
            ],
            "adds": [{"name": "score", "type": "int", "nullable": True}],
        },
        "llm": {"prompt_template": "{text}"},
    })


def _fake_call_llm(reply, per_call_usage: LlmUsage):
    def _call(*_a, usage_out=None, **_k):
        if usage_out is not None:
            usage_out.append(per_call_usage)
        return dict(reply)
    return _call


def test_row_usage_key_never_reaches_stage_output(monkeypatch):
    monkeypatch.setattr(lt, "call_llm", _fake_call_llm(
        {"score": 5}, LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)))
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(place_stage(_llm_stage(), load={"columns": [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "text", "type": "str", "nullable": True}]}), {"load": pd.DataFrame({"id": ["r1", "r2"], "text": ["a", "b"]})}, ctx)
    assert lt.ROW_USAGE_KEY not in out.columns
    assert list(out.columns) == ["id", "text", "score"]


def test_row_usage_sums_across_rows_into_ctx(monkeypatch):
    monkeypatch.setattr(lt, "call_llm", _fake_call_llm(
        {"score": 5}, LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)))
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(place_stage(_llm_stage(), load={"columns": [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "text", "type": "str", "nullable": True}]}), {"load": pd.DataFrame({"id": ["r1", "r2", "r3"], "text": ["a", "b", "c"]})}, ctx)
    assert contribution_of(out).llm_usage == LlmUsage(
        input_tokens=30, output_tokens=12, cost_usd=0.003, calls=3)


def test_run_manifest_records_stage_llm_usage(tmp_path, monkeypatch):
    import json

    from app.runtime.runner import execute_run
    from app.services import versioning
    from app.services.project import save_working_copy_as_version

    monkeypatch.setattr(lt, "call_llm", _fake_call_llm(
        {"score": 5}, LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)))

    (tmp_path / "compiled").mkdir(parents=True)
    (tmp_path / "data").mkdir(parents=True)
    pd.DataFrame({"id": ["a", "b"], "text": ["x", "y"]}).to_csv(
        tmp_path / "data" / "in.csv", index=False)
    load = {"id": "load", "description": "Load", "type": "input_data",
            "connector": {"kind": "file", "params": {
                "path": str(tmp_path / "data" / "in.csv"), "format": "csv"}},
            "signature": {
                "form": "replaces",
                "produces": [
                    {"name": "id", "type": "str", "nullable": True},
                    {"name": "text", "type": "str", "nullable": True},
                ],
            }}
    classify = {"id": "classify", "description": "Classify", "type": "llm_transform",
                "inputs": [{"id": "load"}],
                "signature": {
                    "form": "extends",
                    "reads": [
                        {
                            "input": "load",
                            "columns": [{"name": "text", "type": "str", "nullable": True}],
                        },
                    ],
                    "adds": [{"name": "score", "type": "int", "nullable": True}],
                },
                "llm": {"prompt_template": "{text}"}}
    (tmp_path / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (tmp_path / "compiled" / "02_classify.json").write_text(json.dumps(classify), encoding="utf-8")
    vid = save_working_copy_as_version(tmp_path, message="seed", reviewer="test").version_id
    versioning.publish_version(tmp_path, vid, reviewer="human")

    manifest = execute_run(tmp_path, tmp_path, *pinned_stages(tmp_path))

    assert manifest["status"] == "ok", manifest
    record = next(r for r in manifest["stage_records"] if r["stage_id"] == "classify")
    assert record["llm_usage"] == {
        "input_tokens": 20, "output_tokens": 8, "cost_usd": 0.002, "calls": 2,
    }
    # The non-LLM load stage carries no usage key at all (never a zero).
    load_rec = next(r for r in manifest["stage_records"] if r["stage_id"] == "load")
    assert "llm_usage" not in load_rec


def test_failed_row_still_records_the_tokens_it_spent(monkeypatch):
    def _call(*_a, usage_out=None, **_k):
        if usage_out is not None:
            usage_out.append(LlmUsage(input_tokens=8, output_tokens=0, cost_usd=0.0005, calls=1))
        raise RuntimeError("boom")
    monkeypatch.setattr(lt, "call_llm", _call)
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(place_stage(_llm_stage(), load={"columns": [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "text", "type": "str", "nullable": True}]}), {"load": pd.DataFrame({"id": ["r1"], "text": ["a"]})}, ctx)
    assert contribution_of(out).llm_usage == LlmUsage(
        input_tokens=8, output_tokens=0, cost_usd=0.0005, calls=1)
