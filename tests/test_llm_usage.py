from __future__ import annotations

import pandas as pd

import app.runtime.stages.llm_transform as lt
from app.core.agent.usage import LlmUsage
from app.models import Stage
from app.models.stage import StageType
from app.runtime.stages import HANDLERS
from conftest import contribution_of, make_run_context


def test_summed_adds_fields_and_counts_calls():
    a = LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)
    b = LlmUsage(input_tokens=7, output_tokens=3, cost_usd=0.002, calls=1)
    assert LlmUsage.summed([a, b]) == LlmUsage(
        input_tokens=17, output_tokens=7, cost_usd=0.003, calls=2)


def test_summed_of_nothing_is_the_zero_instance():
    assert LlmUsage.summed([]) == LlmUsage()


def _llm_stage() -> Stage:
    return Stage.model_validate({
        "id": "classify", "name": "Classify", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
            "primary_key": ["id"],
        }}],
        "output_schema": {"columns": [
            {"name": "id", "type": "str"}, {"name": "text", "type": "str"},
            {"name": "score", "type": "int", "nullable": True},
        ], "primary_key": ["id"]},
        "llm": {"prompt_template": "{text}"},
    })


def _fake_call_llm(reply, per_call_usage: LlmUsage):
    """A call_llm stand-in: returns `reply`, and appends `per_call_usage` to the
    caller's usage_out sink (as the real call_llm does on each attempt)."""
    def _call(*_a, usage_out=None, **_k):
        if usage_out is not None:
            usage_out.append(per_call_usage)
        return dict(reply)
    return _call


def test_row_usage_key_never_reaches_stage_output(monkeypatch):
    monkeypatch.setattr(lt, "call_llm", _fake_call_llm(
        {"score": 5}, LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)))
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(
        _llm_stage(), {"load": pd.DataFrame({"id": ["r1", "r2"], "text": ["a", "b"]})}, ctx)
    assert lt.ROW_USAGE_KEY not in out.columns
    assert list(out.columns) == ["id", "text", "score"]


def test_row_usage_sums_across_rows_into_ctx(monkeypatch):
    monkeypatch.setattr(lt, "call_llm", _fake_call_llm(
        {"score": 5}, LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)))
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(
        _llm_stage(), {"load": pd.DataFrame({"id": ["r1", "r2", "r3"], "text": ["a", "b", "c"]})}, ctx)
    assert contribution_of(out).llm_usage == LlmUsage(
        input_tokens=30, output_tokens=12, cost_usd=0.003, calls=3)


def test_run_manifest_records_stage_llm_usage(tmp_path, monkeypatch):
    # A full run: an llm_transform stage's summed usage lands on its manifest
    # record (and thus on disk), where the run's stage panel reads it.
    import json

    from app.runtime.runner import execute_run
    from app.services import versioning
    from app.services.versioning import create_version_from_disk

    monkeypatch.setattr(lt, "call_llm", _fake_call_llm(
        {"score": 5}, LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)))

    (tmp_path / "compiled").mkdir(parents=True)
    (tmp_path / "data").mkdir(parents=True)
    pd.DataFrame({"id": ["a", "b"], "text": ["x", "y"]}).to_csv(
        tmp_path / "data" / "in.csv", index=False)
    load = {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file", "params": {
                "path": str(tmp_path / "data" / "in.csv"), "format": "csv"}},
            "output_schema": {"columns": [
                {"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
                "primary_key": ["id"]}}
    classify = {"id": "classify", "name": "Classify", "type": "llm_transform",
                "inputs": [{"id": "load", "schema": {"columns": [
                    {"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
                    "primary_key": ["id"]}}],
                "output_schema": {"columns": [
                    {"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                    {"name": "score", "type": "int", "nullable": True}],
                    "primary_key": ["id"]},
                "llm": {"prompt_template": "{text}"}}
    (tmp_path / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (tmp_path / "compiled" / "02_classify.json").write_text(json.dumps(classify), encoding="utf-8")
    vid = create_version_from_disk(tmp_path, message="seed", reviewer="test").version_id
    versioning.publish_version(tmp_path, vid, reviewer="human")

    manifest = execute_run(tmp_path, repo_root=tmp_path)

    assert manifest["status"] == "ok", manifest
    record = next(r for r in manifest["stage_records"] if r["stage_id"] == "classify")
    assert record["llm_usage"] == {
        "input_tokens": 20, "output_tokens": 8, "cost_usd": 0.002, "calls": 2,
    }
    # The non-LLM load stage carries no usage key at all (never a zero).
    load_rec = next(r for r in manifest["stage_records"] if r["stage_id"] == "load")
    assert "llm_usage" not in load_rec


def test_failed_row_still_records_the_tokens_it_spent(monkeypatch):
    # A row whose call raises after spending tokens (e.g. a rejected schema then a
    # timeout) must still count those tokens — usage is not only successful calls.
    def _call(*_a, usage_out=None, **_k):
        if usage_out is not None:
            usage_out.append(LlmUsage(input_tokens=8, output_tokens=0, cost_usd=0.0005, calls=1))
        raise RuntimeError("boom")
    monkeypatch.setattr(lt, "call_llm", _call)
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(
        _llm_stage(), {"load": pd.DataFrame({"id": ["r1"], "text": ["a"]})}, ctx)
    assert contribution_of(out).llm_usage == LlmUsage(
        input_tokens=8, output_tokens=0, cost_usd=0.0005, calls=1)
