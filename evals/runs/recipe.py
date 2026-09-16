from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.json_types import JsonScalar
from app.core.run_status import RunStatus

RECIPE_FILE = "run.json"
ARCHIVE_FILE = "project.zip"


class RepoPathLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["repo_path"] = "repo_path"
    path: str


class SuppliedLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["supplied"] = "supplied"


InputLocation = Annotated[RepoPathLocation | SuppliedLocation, Field(discriminator="kind")]


class RecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_id: str
    filename: str
    bytes: int
    sha256: str
    at: InputLocation


class RecipeFigure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    stage_id: str
    value: JsonScalar
    claimable: bool


class CapturedFrom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    workflow_version: str
    captured_at: str
    code_commit: str


class RunRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_run_id: str
    captured: CapturedFrom
    archive_sha256: str
    inputs: list[RecipeInput]
    limits: dict[str, int]
    offsets: dict[str, int]
    ends: RunStatus
    figures: list[RecipeFigure]


def read_recipe(run_dir: Path) -> RunRecipe:
    return RunRecipe.model_validate_json((run_dir / RECIPE_FILE).read_text(encoding="utf-8"))


def write_recipe(run_dir: Path, recipe: RunRecipe) -> None:
    text = recipe.model_dump_json(indent=2) + "\n"
    (run_dir / RECIPE_FILE).write_text(text, encoding="utf-8", newline="\n")


def read_head_commit(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, check=True
    )
    return completed.stdout.decode("utf-8").strip()
