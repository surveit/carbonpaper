"""Task-4 tests: the authoring-loop MCP tools (record_seeds, smoke_run,
read_run_result, start_full_run) over the app.services.authoring_loop logic.

Tools are exercised as the registered callables on `app.mcp.server`, against a
tmp workspace (workspace.EXAMPLES_DIR monkeypatched) and, where a run is
started, the in-memory `FakeRunTool` from test_run_tool. The default
`StubRunTool` is asserted to refuse to fabricate a run id.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.mcp import server
from app.models.run_manifest import RunManifest
from app.services import authoring_loop, versioning, workspace
from tests.test_run_tool import FakeRunTool

_CONNECTOR_A = {"id": "load_a", "name": "A", "type": "input_data",
                "connector": {"kind": "file"}}
_CONNECTOR_B = {"id": "load_b", "name": "B", "type": "input_data",
                "connector": {"kind": "file"}}


@pytest.fixture(autouse=True)
def _restore_run_tool():
    """Each test gets the default StubRunTool back — set_run_tool mutates module
    state that would otherwise leak between tests."""
    original = authoring_loop.get_run_tool()
    yield
    authoring_loop.set_run_tool(original)


def _make_version(project_dir: Path, stages: list[dict]) -> str:
    version = versioning.create_version_from_stages(
        project_dir, stages, message="v", reviewer="tester"
    )
    return version.version_id


def _write_manifest(project_dir: Path, run_id: str, manifest: dict) -> None:
    run_dir = project_dir / "runs" / run_id
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _write_output(project_dir: Path, run_id: str, stage_id: str, frame: pd.DataFrame) -> None:
    outputs = project_dir / "runs" / run_id / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(outputs / f"{stage_id}.parquet", index=False)


def test_smoke_run_with_stub_returns_error_not_a_fabricated_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    pdir = tmp_path / "trail"
    pdir.mkdir()
    version_id = _make_version(pdir, [_CONNECTOR_A])

    # Default StubRunTool cannot start runs — smoke_run surfaces that loudly-as-data.
    result = server.smoke_run(project_id="trail", version_id=version_id, limit=5)
    assert result["ok"] is False
    assert "run_id" not in result
    assert result["error"]


def test_smoke_run_slices_every_connector_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    pdir = tmp_path / "trail"
    pdir.mkdir()
    version_id = _make_version(pdir, [_CONNECTOR_A, _CONNECTOR_B])

    fake = FakeRunTool()
    # FakeRunTool.start_run returns the first registered manifest id, so register one.
    fake.add_run("run_smoke", RunManifest.model_validate(
        {"run_id": "run_smoke", "status": "ok", "workflow_version": version_id, "stages": []}
    ))
    authoring_loop.set_run_tool(fake)

    result = server.smoke_run(project_id="trail", version_id=version_id, limit=5, offset=2)
    assert result["ok"] is True
    assert result["run_id"] == "run_smoke"
    # Every connector stage capped and offset; the version's two connectors are keyed.
    version_id_seen, limits, offsets = fake.started[0]
    assert version_id_seen == version_id
    assert limits == {"load_a": 5, "load_b": 5}
    assert offsets == {"load_a": 2, "load_b": 2}


def test_read_run_result_reports_status_usage_and_failing_seeds(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    pdir = tmp_path / "trail"
    pdir.mkdir()
    # A version whose single connector "load" feeds a flagging stage "flag".
    version_id = _make_version(pdir, [
        {"id": "load", "name": "Load", "type": "input_data", "connector": {"kind": "file"}},
    ])
    run_id = "20260722T120000"
    _write_manifest(pdir, run_id, {
        "run_id": run_id,
        "status": "ok",
        "workflow_version": version_id,
        "stages": [
            {"stage_id": "load", "status": "ok", "rows": 3},
            {"stage_id": "flag", "status": "ok", "rows": 1,
             "llm_usage": {"input_tokens": 100, "output_tokens": 20,
                           "cost_usd": 0.5, "calls": 3}},
        ],
    })
    # The run's input corpus (first connector output) and the flagged rows.
    _write_output(pdir, run_id, "load",
                  pd.DataFrame({"entity_id": ["E1", "E2", "E3"], "amount": [1, 2, 3]}))
    _write_output(pdir, run_id, "flag",
                  pd.DataFrame({"entity_id": ["E2"], "amount": [2]}))

    # E1 must be caught (it wasn't) and E2 must NOT be (it was) — both fail.
    server.record_seeds(project_id="trail", key_column="entity_id", seeds_json=json.dumps([
        {"row_key": "E1", "outcome": "must_catch"},
        {"row_key": "E2", "outcome": "must_not_catch"},
    ]))

    result = server.read_run_result(
        project_id="trail", run_id=run_id,
        positive_column="entity_id", positive_stage_id="flag",
    )
    assert result["status"] == "ok"
    assert result["run_url"] == f"/project/trail/runs/{run_id}"
    assert result["total_usage"]["cost_usd"] == 0.5
    assert result["seeds_checked"] is True
    assert result["staleness_checked"] is True
    assert any("E1" in m for m in result["failing_seeds"])
    assert any("E2" in m for m in result["failing_seeds"])


def test_read_run_result_without_positive_stage_skips_seed_check(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    pdir = tmp_path / "trail"
    pdir.mkdir()
    run_id = "20260722T130000"
    _write_manifest(pdir, run_id, {
        "run_id": run_id, "status": "ok", "workflow_version": "v",
        "stages": [{"stage_id": "load", "status": "ok", "rows": 3}],
    })

    result = server.read_run_result(project_id="trail", run_id=run_id)
    assert result["seeds_checked"] is False
    assert result["staleness_checked"] is False
    assert result["failing_seeds"] == []


def test_read_run_result_staleness_not_checked_when_key_column_absent_from_corpus(
    tmp_path, monkeypatch
):
    """`positive_column` names a column that the run's own connector output does
    not have — seeds are still graded for pass/fail (`seeds_checked` True), but
    staleness against the corpus cannot be assessed, so `staleness_checked` must
    be False rather than silently reading as "checked and clean"."""
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    pdir = tmp_path / "trail"
    pdir.mkdir()
    version_id = _make_version(pdir, [
        {"id": "load", "name": "Load", "type": "input_data", "connector": {"kind": "file"}},
    ])
    run_id = "20260722T140000"
    _write_manifest(pdir, run_id, {
        "run_id": run_id,
        "status": "ok",
        "workflow_version": version_id,
        "stages": [
            {"stage_id": "load", "status": "ok", "rows": 3},
            {"stage_id": "flag", "status": "ok", "rows": 1},
        ],
    })
    # The corpus has no "entity_id" column — only "other_key".
    _write_output(pdir, run_id, "load",
                  pd.DataFrame({"other_key": ["E1", "E2", "E3"], "amount": [1, 2, 3]}))
    _write_output(pdir, run_id, "flag",
                  pd.DataFrame({"entity_id": ["E2"], "amount": [2]}))

    server.record_seeds(project_id="trail", key_column="other_key", seeds_json=json.dumps([
        {"row_key": "E1", "outcome": "must_catch"},
    ]))

    result = server.read_run_result(
        project_id="trail", run_id=run_id,
        positive_column="entity_id", positive_stage_id="flag",
    )
    assert result["seeds_checked"] is True
    assert result["staleness_checked"] is False


def test_mcp_tools_registered():
    """The authoring-loop tools must be present in the FastMCP tool registry, so
    an MCP client can actually discover and call them."""
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    expected = {"record_seeds", "smoke_run", "read_run_result", "start_full_run"}
    assert expected <= registered


def test_start_full_run_is_unsliced(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    pdir = tmp_path / "trail"
    pdir.mkdir()

    fake = FakeRunTool()
    fake.add_run("run_full", RunManifest.model_validate(
        {"run_id": "run_full", "status": "ok", "workflow_version": "v", "stages": []}
    ))
    authoring_loop.set_run_tool(fake)

    result = server.start_full_run(project_id="trail", version_id="v")
    assert result["ok"] is True
    assert result["run_id"] == "run_full"
    _, limits, offsets = fake.started[0]
    assert limits is None
    assert offsets is None
