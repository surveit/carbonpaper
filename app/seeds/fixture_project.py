"""Import a committed fixture as a project, filling in the file paths it ships without.
A fixture cannot record absolute paths — it is committed and the workspace moves — so an
input stage's connector path is empty and a stage's code carries a placeholder token.
Both are filled from files committed beside it, before the document is validated.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.models.review_guide import ReviewGuideDraft
from app.services import project as project_service, run as run_service
from app.services.project import WorkflowFile, import_project


class FixtureFiles(BaseModel):
    """What a fixture ships beside itself, keyed by where each piece belongs."""

    inputs: dict[str, Path]  # stage id -> the data file that stage reads
    code_files: dict[str, Path] = {}  # placeholder in a stage's code -> the file it means
    review_guide: Path | None = None


class SeededProject(BaseModel):
    name: str
    version_id: str


def import_fixture_as_project(
    fixture: Path, files: FixtureFiles, *, name: str
) -> SeededProject:
    """Every file is checked before anything is written, so a missing one fails here."""
    document = read_fixture_document(fixture, files)
    created = import_project(document, name=name)
    version_id = run_service.resolve_version(created, None)
    if files.review_guide is not None:
        # A WorkflowFile carries no review state (#135), so a fixture's guide is
        # committed separately and stored against the version import_project minted.
        project_service.write_review_guide(
            created,
            version_id,
            ReviewGuideDraft.model_validate_json(
                files.review_guide.read_text(encoding="utf-8")
            ),
        )
    return SeededProject(name=created, version_id=version_id)


def _refuse_missing_files(fixture: Path, files: FixtureFiles) -> None:
    wanted = [fixture, *files.inputs.values(), *files.code_files.values()]
    if files.review_guide is not None:
        wanted.append(files.review_guide)
    for path in wanted:
        if not path.is_file():
            raise FileNotFoundError(f"a file the fixture {fixture.name} needs is missing: {path}")


def read_fixture_document(fixture: Path, files: FixtureFiles) -> WorkflowFile:
    """The fixture as a valid document, every path filled — nothing is written."""
    _refuse_missing_files(fixture, files)
    # The raw JSON is the one place dicts are unavoidable: the paths are what MAKE it a
    # valid document, so it cannot be parsed into WorkflowFile until they are in.
    raw: dict[str, Any] = json.loads(fixture.read_text(encoding="utf-8"))
    filled: set[str] = set()
    raw["stages"] = [
        _stage_with_paths_filled_in(stage, files, filled) for stage in raw["stages"]
    ]
    unused = set(files.code_files) - filled
    if unused:
        # The fixture stopped carrying a placeholder, so the file it stood for would
        # silently never be read. Louder here than as a stage failing mid-run.
        raise ValueError(f"{fixture.name} carries none of these placeholders: {sorted(unused)}")
    # Validated rather than patched in place: Connector refuses a relative params.path,
    # so a path no run could resolve fails here instead of at the first stage of the run.
    return WorkflowFile.model_validate(raw)


def _stage_with_paths_filled_in(
    stage: dict[str, Any], files: FixtureFiles, filled: set[str]
) -> dict[str, Any]:
    data_file = files.inputs.get(str(stage.get("id")))
    if data_file is not None:
        connector = stage["connector"]
        params = {**connector.get("params", {}), "path": str(data_file)}
        return {**stage, "connector": {**connector, "params": params}}
    if "function" not in stage:
        return stage
    function = stage["function"]
    code: str = function.get("code") or ""
    for token, path in files.code_files.items():
        if token not in code:
            continue
        # as_posix: this lands in a Python string literal, where a backslash escapes.
        code = code.replace(token, path.as_posix())
        filled.add(token)
    return {**stage, "function": {**function, "code": code}}
