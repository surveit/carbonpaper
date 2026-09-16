from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.core.paths import CARBON_PAPER_HOME, repo_root
from app.core.store_config import configure_default_stores, refuse_renamed_env_vars
from app.services.workspace import configure_projects_dir_from_env
from evals.runs.capture import CaptureRefused, capture_run
from evals.runs.rebuild import RebuiltRun, RunRefused, rebuild_run
from evals.runs.recipe import read_head_commit, read_recipe
from evals.runs.workspace import WorkspaceOutsidePass, configure_throwaway_workspace

_CAPTURE = "capture"
_REBUILD = "rebuild"
_SAVED_RUNS_FOLDER = ("evals", "runs")
_STORAGE_HOME = "the storage home"
_STORAGE_OVERRIDES = (
    "CARBON_PAPER_DB_PATH",
    "CARBON_PAPER_FRAMES_ROOT",
    "CARBON_PAPER_FILES_ROOT",
    "CARBON_PAPER_PROJECTS_DIR",
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == _CAPTURE:
        return _capture_from_the_machines_stores(args.project, args.run, replace=args.replace)
    return _rebuild_in_a_throwaway_workspace(
        args.run_dir, workspace=args.workspace, inputs_dir=args.inputs_dir
    )


def _capture_from_the_machines_stores(project_id: str, run_id: str, *, replace: bool) -> int:
    refuse_renamed_env_vars()
    configure_projects_dir_from_env()
    configure_default_stores()
    root = repo_root()
    try:
        saved = capture_run(
            project_id, run_id, root.joinpath(*_SAVED_RUNS_FOLDER), repo_root=root, replace=replace
        )
    except CaptureRefused as refused:
        _print_refusal(str(refused))
        return 1
    print(f"saved to {saved}")
    return 0


def _rebuild_in_a_throwaway_workspace(
    run_dir: Path, *, workspace: Path, inputs_dir: Path | None
) -> int:
    holding = _find_real_storage_holding(workspace)
    if holding:
        _print_refusal(_describe_a_workspace_inside_real_storage(workspace, holding))
        return 1
    try:
        configure_throwaway_workspace(workspace)
        rebuilt = rebuild_run(run_dir, repo_root=repo_root(), inputs_dir=inputs_dir)
    except (WorkspaceOutsidePass, RunRefused) as refused:
        _print_refusal(str(refused))
        return 1
    print(_describe_the_rebuilt_run(run_dir, rebuilt))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.runs",
        description="Save a finished run as a saved run, or rebuild a saved run.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser(_CAPTURE, help="save a finished run of the machine's stores")
    capture.add_argument("--project", required=True, help="the project the run belongs to")
    capture.add_argument("--run", required=True, help="the run to save")
    capture.add_argument("--replace", action="store_true", help="rewrite the saved run already there")
    rebuild = commands.add_parser(_REBUILD, help="run a saved run again in a throwaway workspace")
    rebuild.add_argument("run_dir", type=Path, help="the saved run's folder")
    rebuild.add_argument("--workspace", type=Path, required=True, help="a throwaway folder to run in")
    rebuild.add_argument("--inputs-dir", type=Path, help="the folder holding the run's input files")
    return parser.parse_args(argv)


def _find_real_storage_holding(workspace: Path) -> list[str]:
    resolved = workspace.resolve()
    return [
        f"{label}: {path}"
        for label, path in _find_real_storage_locations()
        if resolved.is_relative_to(path)
    ]


def _find_real_storage_locations() -> list[tuple[str, Path]]:
    return [
        (_STORAGE_HOME, CARBON_PAPER_HOME.resolve()),
        *[
            (name, Path(value).resolve())
            for name in _STORAGE_OVERRIDES
            if (value := os.environ.get(name))
        ],
    ]


def _describe_a_workspace_inside_real_storage(workspace: Path, holding: list[str]) -> str:
    return (
        f"a rebuild needs a throwaway workspace, and {workspace.resolve()} sits inside the "
        "machine's real storage:\n  " + "\n  ".join(holding)
    )


def _describe_the_rebuilt_run(run_dir: Path, rebuilt: RebuiltRun) -> str:
    return (
        f"rebuilt run '{rebuilt.run_id}' of project '{rebuilt.project_id}'\n"
        f"captured at commit {read_recipe(run_dir).captured.code_commit}, "
        f"rebuilt at commit {read_head_commit(repo_root())}"
    )


def _print_refusal(reason: str) -> None:
    print(reason, file=sys.stderr)
