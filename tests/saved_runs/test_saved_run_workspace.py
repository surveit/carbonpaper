from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.frames import FrameStore, configure_frame_store, get_frame_store
from app.core.persistence import get_store
from app.core.run_status import RunStatus
from app.core.sqlite_store import SqliteKvStore
from app.services.workspace import projects_dir, set_projects_dir
from evals.runs.recipe import (
    RECIPE_FILE,
    CapturedFrom,
    RecipeFigure,
    RecipeInput,
    RepoPathLocation,
    RunRecipe,
    SuppliedLocation,
    read_recipe,
    write_recipe,
)
from evals.runs.workspace import (
    WorkspaceOutsidePass,
    configure_throwaway_workspace,
    validate_workspace_is_under,
)

_STORE_LEAF_NAMES = {"database": "app.db", "frame store": "frames", "files root": "files", "projects dir": "examples"}
_EXTRA_FIELD_ERRORS: set[tuple[str, tuple[int | str, ...]]] = {
    ("extra_forbidden", location)
    for location in [
        ("unexpected",),
        ("captured", "unexpected"),
        ("inputs", 0, "unexpected"),
        ("inputs", 0, "at", "supplied", "unexpected"),
        ("inputs", 1, "unexpected"),
        ("inputs", 1, "at", "repo_path", "unexpected"),
        *[("figures", index, "unexpected") for index in range(4)],
    ]
}


def test_a_throwaway_workspace_points_every_root_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "pass").resolve()
    root.mkdir()
    monkeypatch.chdir(tmp_path)
    configure_throwaway_workspace(Path("pass"))
    get_store().write("probe", "document", {"written": "through the configured store"})
    assert SqliteKvStore(str(root / "app.db")).read("probe", "document") == {
        "written": "through the configured store"
    }
    assert get_frame_store().root == root / "frames"
    assert os.environ["CARBON_PAPER_FILES_ROOT"] == str(root / "files")
    assert projects_dir() == root / "examples"


def test_a_throwaway_workspace_creates_its_missing_root_and_parents(tmp_path: Path) -> None:
    root = tmp_path / "missing" / "pass"
    configure_throwaway_workspace(root)
    assert (root / "app.db").is_file()


def test_configuring_a_throwaway_workspace_refuses_a_store_left_outside_its_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evals.runs.workspace.set_projects_dir", lambda path: None)
    with pytest.raises(WorkspaceOutsidePass) as refused:
        configure_throwaway_workspace(tmp_path / "pass")
    assert f"projects dir: {(tmp_path / 'examples').resolve()}" in str(refused.value)


def test_a_workspace_whose_file_root_is_elsewhere_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pass"
    configure_frame_store(FrameStore(root / "frames"))
    set_projects_dir(root / "examples")
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "elsewhere" / "files"))
    with pytest.raises(WorkspaceOutsidePass):
        validate_workspace_is_under(root, root / "app.db")


@pytest.mark.parametrize("outside", [{"database", "files root"}, {"frame store", "projects dir"}])
def test_validation_lists_exactly_the_roots_outside_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: set[str]
) -> None:
    root = tmp_path / "pass"
    stores = {
        label: (tmp_path / "elsewhere" if label in outside else root) / leaf_name
        for label, leaf_name in _STORE_LEAF_NAMES.items()
    }
    configure_frame_store(FrameStore(stores["frame store"]))
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(stores["files root"]))
    set_projects_dir(stores["projects dir"])
    with pytest.raises(WorkspaceOutsidePass) as refused:
        validate_workspace_is_under(root, stores["database"])
    named = {label for label, path in stores.items() if str(path.resolve()) in str(refused.value)}
    assert named == outside


def test_a_recipe_round_trips_through_run_json(tmp_path: Path) -> None:
    recipe = _build_recipe()
    write_recipe(tmp_path, recipe)
    written = (tmp_path / RECIPE_FILE).read_bytes().decode("utf-8")
    assert "données.csv" in written
    assert written == json.dumps(json.loads(written), indent=2, ensure_ascii=False) + "\n"
    reread = read_recipe(tmp_path)
    assert reread == recipe
    assert reread.model_dump_json(indent=2) + "\n" == written


def test_every_recipe_model_refuses_an_unknown_field(tmp_path: Path) -> None:
    payload = _build_recipe().model_dump(mode="json")
    for model_object in [payload, payload["captured"], *payload["inputs"], *payload["figures"]]:
        model_object["unexpected"] = True
    for recipe_input in payload["inputs"]:
        recipe_input["at"]["unexpected"] = True
    (tmp_path / RECIPE_FILE).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError) as refused:
        read_recipe(tmp_path)
    assert {(error["type"], error["loc"]) for error in refused.value.errors()} == _EXTRA_FIELD_ERRORS


def _build_recipe() -> RunRecipe:
    return RunRecipe(
        workflow_run_id="run-1",
        captured=CapturedFrom(
            project_id="tiny",
            workflow_version="version-1",
            captured_at="2026-09-15T12:00:00+00:00",
            code_commit="abc1234",
        ),
        inputs=[
            RecipeInput(stage_id="load", filename="données.csv", bytes=12, sha256="a" * 64, at=SuppliedLocation()),
            RecipeInput(
                stage_id="lookup",
                filename="codes.csv",
                bytes=3,
                sha256="b" * 64,
                at=RepoPathLocation(path="evals/runs/run-1/codes.csv"),
            ),
        ],
        limits={"load": 10},
        offsets={"load": 2},
        ends=RunStatus.AWAITING_REVIEW,
        figures=[
            RecipeFigure(slug=f"figure-{index}", stage_id="totals", value=value, claimable=index == 0)
            for index, value in enumerate([3, 0.25, True, None])
        ],
    )
