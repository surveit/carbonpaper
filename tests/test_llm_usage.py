from __future__ import annotations

import pandas as pd
import pytest

import app.runtime.stages.llm_transform as lt
from app.core.agent.usage import LlmUsage
from app.models import parse_stage, Stage
from app.models.stage import StageType
from app.runtime.stages import HANDLERS
from conftest import (
    as_inputs, contribution_of, make_run_context, pinned_stages, place_stage, rows_of,
)
from stage_seed import add_stage


def test_summed_adds_fields_and_counts_calls():
    a = LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)
    b = LlmUsage(input_tokens=7, output_tokens=3, cost_usd=0.002, calls=1)
    assert LlmUsage.summed([a, b]) == LlmUsage(
        input_tokens=17, output_tokens=7, cost_usd=0.003, calls=2)


def test_summed_of_nothing_is_the_zero_instance():
    assert LlmUsage.summed([]) == LlmUsage()


def test_the_model_survives_being_summed_with_the_zero_usages_beside_it():
    # The batch path lands a chunk's whole usage on its first row and zeroes the rest.
    paid = LlmUsage(cost_usd=0.01, calls=1, model="claude-haiku-4-5")
    assert LlmUsage.summed([paid, LlmUsage(), LlmUsage()]).model == "claude-haiku-4-5"


def test_totalling_two_models_raises_rather_than_keeping_one():
    haiku = LlmUsage(calls=1, model="claude-haiku-4-5")
    opus = LlmUsage(calls=1, model="claude-opus-5")
    with pytest.raises(ValueError, match="two models"):
        LlmUsage.summed([haiku, opus])


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
    out = HANDLERS[StageType.llm_transform].execute(
        place_stage(
            _llm_stage(), load={"columns": [
            {"name": "id", "type": "str", "nullable": True},
            {"name": "text", "type": "str", "nullable": True}]}), as_inputs({"load": pd.DataFrame({"id": ["r1", "r2"], "text": ["a", "b"]})}), ctx)
    assert lt.ROW_USAGE_KEY not in rows_of(out).columns
    assert list(rows_of(out).columns) == ["id", "text", "score"]


def test_row_usage_sums_across_rows_into_ctx(monkeypatch):
    monkeypatch.setattr(lt, "call_llm", _fake_call_llm(
        {"score": 5}, LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)))
    ctx = make_run_context()
    out = HANDLERS[StageType.llm_transform].execute(
        place_stage(
            _llm_stage(), load={"columns": [
            {"name": "id", "type": "str", "nullable": True},
            {"name": "text", "type": "str", "nullable": True}]}), as_inputs({"load": pd.DataFrame({"id": ["r1", "r2", "r3"], "text": ["a", "b", "c"]})}), ctx)
    assert contribution_of(out).llm_usage == LlmUsage(
        input_tokens=30, output_tokens=12, cost_usd=0.003, calls=3)


def test_run_manifest_records_stage_llm_usage(tmp_path, monkeypatch):

    from app.runtime.runner import execute_run
    from app.services.project import save_working_copy_as_version

    monkeypatch.setattr(lt, "call_llm", _fake_call_llm(
        {"score": 5}, LlmUsage(input_tokens=10, output_tokens=4, cost_usd=0.001, calls=1)))

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
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
    add_stage(tmp_path, load)
    add_stage(tmp_path, classify)
    save_working_copy_as_version(tmp_path.name, message="seed")

    manifest = execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path))

    assert manifest["status"] == "ok", manifest
    record = next(r for r in manifest["stage_records"] if r["stage_id"] == "classify")
    # `model` is None because the stub replaces call_llm, which is where the runtime
    # resolves and stamps it — the manifest reports "not recorded", never a guess.
    assert record["llm_usage"] == {
        "input_tokens": 20, "output_tokens": 8, "cost_usd": 0.002, "calls": 2,
        "model": None,
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
    out = HANDLERS[StageType.llm_transform].execute(
        place_stage(
            _llm_stage(), load={"columns": [
            {"name": "id", "type": "str", "nullable": True},
            {"name": "text", "type": "str", "nullable": True}]}), as_inputs({"load": pd.DataFrame({"id": ["r1"], "text": ["a"]})}), ctx)
    assert contribution_of(out).llm_usage == LlmUsage(
        input_tokens=8, output_tokens=0, cost_usd=0.0005, calls=1)
