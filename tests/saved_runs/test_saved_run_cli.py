from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from app.core.run_status import RunStatus
from app.services.run import list_every_run_entry, read_run_manifest
from evals.runs.capture import CaptureRefused, capture_run
from evals.runs.cli import main
from evals.runs.rebuild import RebuiltRun, RebuiltRunRefused, RunRefused
from evals.runs.workspace import configure_throwaway_workspace
from tiny_run import ROWS_FILENAME, TINY_ROWS, create_tiny_run

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_PREFIX = "CARBON_PAPER_"
_REFUSE_RENAMED_ENV_VARS = "refuse_renamed_env_vars"
_STORE_CONFIGURERS = (
    _REFUSE_RENAMED_ENV_VARS,
    "configure_projects_dir_from_env",
    "configure_default_stores",
)
_CAPTURE_RUN = "capture_run"
_PROJECT = "tiny"
_RUN = "20260915T120000.000000"
_REBUILT = RebuiltRun(project_id="tiny-rebuilt", run_id="20260916T090000.000000")
_CAPTURE_REFUSAL = "run 'run-1' has status 'running'"
_REAL_STORAGE: dict[str, Callable[[pytest.MonkeyPatch, Path], None]] = {
    "the storage home": lambda patch, root: patch.setattr("evals.runs.cli.CARBON_PAPER_HOME", root),
    "CARBON_PAPER_PROJECTS_DIR": lambda patch, root: patch.setenv(
        "CARBON_PAPER_PROJECTS_DIR", str(root)
    ),
}


class _CapturedFrom(NamedTuple):
    project_id: str
    run_id: str
    runs_root: Path
    repo_root: Path
    replace: bool


class _RebuiltFrom(NamedTuple):
    run_dir: Path
    repo_root: Path
    inputs_dir: Path | None


@pytest.fixture(autouse=True)
def store_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """No test here reads the machine's storage config; the files root stays under tmp_path."""
    for name in [name for name in os.environ if name.startswith(_ENV_PREFIX)]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    calls: list[str] = []
    for name in _STORE_CONFIGURERS:
        monkeypatch.setattr(f"evals.runs.cli.{name}", _record_a_call(calls, name))
    return calls


def test_capture_command_reads_the_machines_configured_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_calls: list[str]
) -> None:
    _record_capture_calls(monkeypatch, store_calls, tmp_path / "saved")
    assert main(["capture", "--project", _PROJECT, "--run", _RUN]) == 0
    *configured, captured = store_calls
    assert configured[0] == _REFUSE_RENAMED_ENV_VARS
    assert sorted(configured) == sorted(_STORE_CONFIGURERS)
    assert captured == _CAPTURE_RUN


@pytest.mark.parametrize("replace", [False, True], ids=["without --replace", "with --replace"])
def test_capture_command_hands_capture_the_run_the_repo_and_whether_to_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_calls: list[str], replace: bool
) -> None:
    recorded = _record_capture_calls(monkeypatch, store_calls, tmp_path / "saved")
    replacing = ["--replace"] if replace else []
    assert main(["capture", "--project", _PROJECT, "--run", _RUN, *replacing]) == 0
    assert recorded == [
        _CapturedFrom(
            project_id=_PROJECT,
            run_id=_RUN,
            runs_root=_REPO_ROOT / "evals" / "runs",
            repo_root=_REPO_ROOT,
            replace=replace,
        )
    ]


def test_capture_command_prints_the_folder_it_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_calls: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    saved = tmp_path / "saved" / _RUN
    _record_capture_calls(monkeypatch, store_calls, saved)
    assert main(["capture", "--project", _PROJECT, "--run", _RUN]) == 0
    assert capsys.readouterr().out == f"saved to {saved}\n"


def test_capture_command_prints_the_reason_a_capture_was_refused_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _refuse_every_capture(monkeypatch, CaptureRefused(_CAPTURE_REFUSAL))
    assert main(["capture", "--project", _PROJECT, "--run", _RUN]) == 1
    assert capsys.readouterr().out == f"{_CAPTURE_REFUSAL}\n"


def test_rebuild_command_configures_only_the_named_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_calls: list[str]
) -> None:
    saved = _capture_a_tiny_run(tmp_path)
    configured = _record_configured_workspaces(monkeypatch)
    workspace = tmp_path / "ws"
    assert main(_rebuild_argv(saved, workspace, _write_rows(tmp_path))) == 0
    assert store_calls == []
    assert configured == [workspace]
    assert (workspace / "app.db").is_file()
    [entry] = list_every_run_entry()
    assert read_run_manifest(entry.project, entry.run_id).status == RunStatus.OK


def test_rebuild_command_prints_the_project_and_run_it_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _record_rebuild_calls(monkeypatch)
    assert main(_rebuild_argv(tmp_path / "saved", tmp_path / "ws", None)) == 0
    assert capsys.readouterr().out == (
        f"rebuilt run '{_REBUILT.run_id}' of project '{_REBUILT.project_id}'\n"
    )


@pytest.mark.parametrize("given", [True, False], ids=["with --inputs-dir", "without --inputs-dir"])
def test_rebuild_command_hands_rebuild_the_saved_run_the_repo_and_the_inputs_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: bool
) -> None:
    recorded = _record_rebuild_calls(monkeypatch)
    inputs_dir = _write_rows(tmp_path) if given else None
    saved = tmp_path / "saved" / _RUN
    assert main(_rebuild_argv(saved, tmp_path / "ws", inputs_dir)) == 0
    assert recorded == [
        _RebuiltFrom(run_dir=saved, repo_root=_REPO_ROOT, inputs_dir=inputs_dir)
    ]


def test_rebuild_command_prints_every_reason_a_run_was_refused_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reasons = ["the archive is not the captured one", "the rows are not the captured rows"]
    _refuse_every_rebuild(monkeypatch, RunRefused(reasons))
    assert main(_rebuild_argv(tmp_path / "saved", tmp_path / "ws", None)) == 1
    assert capsys.readouterr().out == "".join(f"{reason}\n" for reason in reasons)


def test_rebuild_command_names_the_run_refused_after_it_was_rebuilt_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reason = "figure 'amount-total': the rebuilt run published 12; the recipe recorded 13"
    _refuse_every_rebuild(monkeypatch, RebuiltRunRefused(_REBUILT, [reason]))
    assert main(_rebuild_argv(tmp_path / "saved", tmp_path / "ws", None)) == 1
    printed = capsys.readouterr().out
    assert _REBUILT.project_id in printed and _REBUILT.run_id in printed
    assert reason in printed


@pytest.mark.parametrize("label", sorted(_REAL_STORAGE))
def test_rebuild_command_refuses_a_workspace_inside_the_machines_real_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
) -> None:
    real_storage = tmp_path / "real_storage"
    _REAL_STORAGE[label](monkeypatch, real_storage)
    configured = _record_configured_workspaces(monkeypatch)
    workspace = real_storage / "ws"
    assert main(_rebuild_argv(tmp_path / "saved", workspace, None)) == 1
    assert configured == []
    printed = capsys.readouterr().out
    assert f"{label}: {real_storage}" in printed and str(workspace) in printed


def test_rebuild_command_prints_a_store_left_outside_the_workspace_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("evals.runs.workspace.set_projects_dir", lambda path: None)
    assert main(_rebuild_argv(tmp_path / "saved", tmp_path / "ws", None)) == 1
    assert "every store of a throwaway workspace must sit under" in capsys.readouterr().out


def test_the_saved_runs_cli_requires_a_command() -> None:
    with pytest.raises(SystemExit) as exited:
        main([])
    assert exited.value.code == 2


def _rebuild_argv(saved: Path, workspace: Path, inputs_dir: Path | None) -> list[str]:
    naming_inputs = [] if inputs_dir is None else ["--inputs-dir", str(inputs_dir)]
    return ["rebuild", str(saved), "--workspace", str(workspace), *naming_inputs]


def _capture_a_tiny_run(tmp_path: Path) -> Path:
    run = create_tiny_run()
    return capture_run(
        run.project_id, run.run_id, tmp_path / "saved", repo_root=_REPO_ROOT, replace=False
    )


def _write_rows(tmp_path: Path) -> Path:
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / ROWS_FILENAME).write_bytes(TINY_ROWS)
    return inputs_dir


def _record_a_call(calls: list[str], name: str) -> Callable[[], None]:
    def record_the_call() -> None:
        calls.append(name)

    return record_the_call


def _record_capture_calls(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], saved: Path
) -> list[_CapturedFrom]:
    recorded: list[_CapturedFrom] = []

    def capture(
        project_id: str, run_id: str, runs_root: Path, *, repo_root: Path, replace: bool
    ) -> Path:
        calls.append(_CAPTURE_RUN)
        recorded.append(_CapturedFrom(project_id, run_id, runs_root, repo_root, replace))
        return saved

    monkeypatch.setattr("evals.runs.cli.capture_run", capture)
    return recorded


def _refuse_every_capture(monkeypatch: pytest.MonkeyPatch, refusal: CaptureRefused) -> None:
    def capture(*_args: object, **_kwargs: object) -> Path:
        raise refusal

    monkeypatch.setattr("evals.runs.cli.capture_run", capture)


def _record_rebuild_calls(monkeypatch: pytest.MonkeyPatch) -> list[_RebuiltFrom]:
    recorded: list[_RebuiltFrom] = []

    def rebuild(run_dir: Path, *, repo_root: Path, inputs_dir: Path | None) -> RebuiltRun:
        recorded.append(_RebuiltFrom(run_dir, repo_root, inputs_dir))
        return _REBUILT

    monkeypatch.setattr("evals.runs.cli.rebuild_run", rebuild)
    return recorded


def _refuse_every_rebuild(monkeypatch: pytest.MonkeyPatch, refusal: RunRefused) -> None:
    def rebuild(*_args: object, **_kwargs: object) -> RebuiltRun:
        raise refusal

    monkeypatch.setattr("evals.runs.cli.rebuild_run", rebuild)


def _record_configured_workspaces(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    configured: list[Path] = []

    def configure(root: Path) -> None:
        configured.append(root)
        configure_throwaway_workspace(root)

    monkeypatch.setattr("evals.runs.cli.configure_throwaway_workspace", configure)
    return configured
