"""Task-3 tests: RunManifest reader, the RunTool seam, and its raising stub.

The manifest fixture copies a minimal slice of the real shape written by
`app.runtime.runner.prepare_run` / `_execute_stages` (run_id, workflow_version,
status, and per-stage records with `stage_id`/`type`/`name`/`status`/`rows` and
an optional dumped `llm_usage`) — plus a few extra keys the reader must ignore.
This test never imports app.runtime; it writes the JSON by hand.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import RunNotFoundError, RunToolUnavailableError
from app.services.run_tool import (
    RunManifest,
    RunTool,
    StubRunTool,
    read_run_manifest,
)


def _write_manifest(project_dir: Path, run_id: str, manifest: dict[str, object]) -> None:
    run_dir = project_dir / "runs" / run_id
    (run_dir).mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _sample_manifest(run_id: str = "run_abc") -> dict[str, object]:
    """A minimal manifest in the real on-disk shape, with extra keys the reader
    must tolerate and one stage carrying a dumped LlmUsage, one without."""
    return {
        "run_id": run_id,
        "started_at": "2026-07-22T10:00:00",
        "project": "demo",
        "workflow_version": "20260722T100000",
        "limit_overrides": {},
        "offset_overrides": {},
        "status": "ok",
        "stages": [
            {
                "stage_id": "load",
                "type": "input",
                "name": "Load corpus",
                "status": "ok",
                "rows": 10,
                "elapsed_ms": 5,
                "error": None,
            },
            {
                "stage_id": "classify",
                "type": "llm_transform",
                "name": "Classify",
                "status": "ok",
                "rows": 10,
                "llm_usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cost_usd": 0.5,
                    "calls": 10,
                },
            },
        ],
    }


def test_run_manifest_parses_real_shape_and_totals_usage(tmp_path: Path) -> None:
    manifest = RunManifest.model_validate(_sample_manifest())

    assert manifest.run_id == "run_abc"
    assert manifest.status == "ok"
    assert manifest.workflow_version == "20260722T100000"
    assert [s.stage_id for s in manifest.stages] == ["load", "classify"]
    # A stage record with no `llm_usage` key -> None; rows read from `rows`.
    assert manifest.stages[0].llm_usage is None
    assert manifest.stages[0].row_count == 10

    total = manifest.total_usage()
    assert total.input_tokens == 100
    assert total.output_tokens == 20
    assert total.cost_usd == 0.5
    assert total.calls == 10


def test_read_run_manifest_reads_on_disk_run(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "run_abc", _sample_manifest())

    manifest = read_run_manifest(tmp_path, "run_abc")
    assert manifest.run_id == "run_abc"
    assert manifest.stages[1].stage_id == "classify"


def test_read_run_manifest_missing_run_raises(tmp_path: Path) -> None:
    with pytest.raises(RunNotFoundError):
        read_run_manifest(tmp_path, "no_such_run")


def test_stub_run_tool_start_raises_status_reads(tmp_path: Path) -> None:
    stub = StubRunTool()

    with pytest.raises(RunToolUnavailableError):
        stub.start_run(tmp_path, version_id="20260722T100000")

    _write_manifest(tmp_path, "run_abc", _sample_manifest())
    manifest = stub.run_status(tmp_path, "run_abc")
    assert isinstance(manifest, RunManifest)
    assert manifest.run_id == "run_abc"


class FakeRunTool:
    """In-memory RunTool for Task 4: records start_run calls and serves manifests
    handed to it, so a consumer can be driven without a real runner or disk."""

    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, int] | None, dict[str, int] | None]] = []
        self._manifests: dict[str, RunManifest] = {}

    def add_run(self, run_id: str, manifest: RunManifest) -> None:
        self._manifests[run_id] = manifest

    def start_run(
        self,
        project_dir: Path,
        *,
        version_id: str,
        limits: dict[str, int] | None = None,
        offsets: dict[str, int] | None = None,
    ) -> str:
        self.started.append((version_id, limits, offsets))
        return next(iter(self._manifests), "run_fake")

    def run_status(self, project_dir: Path, run_id: str) -> RunManifest:
        if run_id not in self._manifests:
            raise RunNotFoundError(run_id)
        return self._manifests[run_id]


def test_fake_run_tool_satisfies_protocol(tmp_path: Path) -> None:
    fake: RunTool = FakeRunTool()
    manifest = RunManifest.model_validate(_sample_manifest())
    fake.add_run("run_abc", manifest)  # type: ignore[attr-defined]

    run_id = fake.start_run(tmp_path, version_id="v1", limits={"load": 3})
    assert run_id == "run_abc"
    assert fake.run_status(tmp_path, "run_abc").run_id == "run_abc"
