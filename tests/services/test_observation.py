"""app.services.observation: name-based observed-value lookup behind the injected
profiler seam. Misses raise (unknown project/stage/column, a stage that is not
input_data, a seam nobody wired); the composition roots (app.web.routers.editing,
app.mcp.server) inject app.runtime.observation.profile_input_stage at import."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import Stage
from app.models.observation import (
    DEFAULT_MAX_DISTINCT_VALUES,
    ColumnValueProfile,
    InputFrameProfile,
)
from app.runtime.observation import profile_input_stage
from app.services import observation
from app.services.errors import InputProfilerNotConfiguredError


def _seed_project(root: Path, name: str, csv_path: Path | None) -> Path:
    """One project whose workflow is a single input_data stage bound to `csv_path`."""
    compiled = root / name / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    params: dict = {} if csv_path is None else {"path": str(csv_path)}
    stage = {
        "id": "load", "name": "Load rows", "type": "input_data",
        "connector": {"kind": "file", "params": params},
        "output_schema": {"columns": [{"name": "status", "type": "str", "nullable": True},
                                      {"name": "zip", "type": "str", "nullable": True}]},
    }
    (compiled / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    return root / name


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "permits.csv"
    path.write_text("status,zip\nfiled,02134\ngranted,90210\nfiled,02134\n",
                    encoding="utf-8")
    return path


@pytest.fixture
def real_profiler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observation, "_input_profiler", profile_input_stage)


# ── the injection seam ───────────────────────────────────────────────────────

def test_unwired_seam_raises_rather_than_fabricating(
    projects_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(projects_root, "permits", _csv(tmp_path))
    monkeypatch.setattr(observation, "_input_profiler", None)
    with pytest.raises(InputProfilerNotConfiguredError):
        observation.observed_column_profile("permits", "load", "status")


def test_web_editing_router_injects_the_runtime_profiler() -> None:
    import app.web.routers.editing  # noqa: F401  (wiring happens at import)
    assert observation._input_profiler is profile_input_stage


def test_mcp_server_injects_the_runtime_profiler() -> None:
    import app.mcp.server  # noqa: F401  (wiring happens at import)
    assert observation._input_profiler is profile_input_stage


def test_stub_profiler_is_called_with_the_resolved_stage(
    projects_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(projects_root, "permits", _csv(tmp_path))
    seen: list[tuple[Stage, int]] = []

    def stub(stage: Stage, max_values: int) -> InputFrameProfile:
        seen.append((stage, max_values))
        return InputFrameProfile(row_count=1, columns=[
            ColumnValueProfile(name="status", row_count=1, null_count=0,
                               distinct_count=1, values=["filed"]),
        ])

    monkeypatch.setattr(observation, "_input_profiler", stub)
    profile = observation.observed_column_profile("permits", "load", "status")
    assert profile.values == ["filed"]
    assert [(stage.id, cap) for stage, cap in seen] == [
        ("load", DEFAULT_MAX_DISTINCT_VALUES)
    ]


def test_caller_maximum_reaches_the_profiler(
    projects_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(projects_root, "permits", _csv(tmp_path))
    seen: list[int] = []

    def stub(stage: Stage, max_values: int) -> InputFrameProfile:
        seen.append(max_values)
        return InputFrameProfile(row_count=1, columns=[
            ColumnValueProfile(name="status", row_count=1, null_count=0,
                               distinct_count=1, values=["filed"]),
        ])

    monkeypatch.setattr(observation, "_input_profiler", stub)
    observation.observed_column_profile("permits", "load", "status", max_values=5000)
    assert seen == [5000]


# ── end to end over a real file ──────────────────────────────────────────────

def test_observed_values_come_from_the_real_file(
    projects_root: Path, tmp_path: Path, real_profiler: None
) -> None:
    _seed_project(projects_root, "permits", _csv(tmp_path))
    profile = observation.observed_column_profile("permits", "load", "status")
    assert profile.values == ["filed", "granted"]
    assert profile.row_count == 3
    assert profile.null_count == 0


# ── loud misses ──────────────────────────────────────────────────────────────

def test_unknown_project_raises(real_profiler: None) -> None:
    with pytest.raises(ValueError, match="no project 'ghost'"):
        observation.observed_column_profile("ghost", "load", "status")


def test_unknown_stage_names_the_real_input_stages(
    projects_root: Path, tmp_path: Path, real_profiler: None
) -> None:
    _seed_project(projects_root, "permits", _csv(tmp_path))
    with pytest.raises(ValueError, match="no stage 'nope'.*load"):
        observation.observed_column_profile("permits", "nope", "status")


def test_unknown_column_names_the_observed_columns(
    projects_root: Path, tmp_path: Path, real_profiler: None
) -> None:
    _seed_project(projects_root, "permits", _csv(tmp_path))
    with pytest.raises(ValueError, match="no column 'nope'.*status"):
        observation.observed_column_profile("permits", "load", "nope")


def test_unbound_input_raises_not_an_empty_profile(
    projects_root: Path, real_profiler: None
) -> None:
    _seed_project(projects_root, "permits", None)
    with pytest.raises(ValueError, match="no file bound"):
        observation.observed_column_profile("permits", "load", "status")
