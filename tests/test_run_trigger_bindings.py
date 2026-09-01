"""POST /project/{name}/run with input-binding form fields, and the runs page rendering
one file PICKER per file-kind input stage. A field carries a stored file's sha256, never
a path; blank means run whatever the workflow authored."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.services.run as run_service
from app.main import app
from app.services import workspace
from app.services.project import save_working_copy_as_version
from app.core.files import (
    FileCompleteness, ProjectFile, list_project_files, save_upload,
    update_file_provenance,
)
from app.core.frames import read_frame_table
from app.web.run_inputs import FileChoice, build_file_choice
from stage_seed import add_stage, read_stage
from run_seed import read_manifest

client = TestClient(app)


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    # output_schema names the CSV's columns; Stage._schemas_declared wants it.
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "signature": {
                 "form": "replaces",
                 "produces": [
                     {"name": "name", "type": "str", "nullable": False},
                     {"name": "val", "type": "int", "nullable": False},
                 ],
             },
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    add_stage(proj, stage)
    save_working_copy_as_version(proj.name, message="seed")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def _store(name: str, frame: pd.DataFrame, tmp_path) -> str:
    """Put a file in the project's store the way the run form's Upload… does."""
    path = tmp_path / name
    if name.endswith(".parquet"):
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)
    with path.open("rb") as handle:
        return save_upload(name, handle, "demo").id


def _manifest(proj):
    run_dir = sorted((proj / "runs").iterdir())[-1]
    return read_manifest(run_dir.parent.parent, run_dir.name)


def test_a_picked_file_becomes_run_binding(project, tmp_path):
    file_id = _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)
    resp = client.post("/project/demo/run",
                       data={"binding__load": file_id}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project)["input_bindings"]["load"]["source"] == "run"


def test_binding_carries_the_bound_files_own_format(project, tmp_path):
    file_id = _store("b.parquet", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)
    resp = client.post("/project/demo/run",
                       data={"binding__load": file_id}, follow_redirects=False)
    assert resp.status_code == 303
    manifest = _manifest(project)
    assert manifest["parameters"]["run_bindings"]["load"]["format"] == "parquet"
    assert manifest["stage_records"][0]["status"] == "ok"


def test_binding_a_file_with_an_unreadable_extension_returns_400(project, tmp_path):
    path = tmp_path / "b.rtf"
    path.write_text("not a table", encoding="utf-8")
    with path.open("rb") as handle:
        file_id = save_upload("b.rtf", handle, "demo").id
    resp = client.post("/project/demo/run",
                       data={"binding__load": file_id}, follow_redirects=False)
    assert resp.status_code == 400
    assert ".rtf" in resp.json()["detail"]
    assert not (project / "runs").exists()


def test_a_file_id_this_project_does_not_hold_returns_400(project):
    resp = client.post("/project/demo/run",
                       data={"binding__load": "0" * 32}, follow_redirects=False)
    assert resp.status_code == 400
    assert "has no file" in resp.json()["detail"]
    assert not (project / "runs").exists()


def test_picking_nothing_stays_workflow_source(project):
    # Blank is the picker's first option — "the path this workflow names".
    resp = client.post("/project/demo/run",
                       data={"binding__load": ""}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project)["input_bindings"]["load"]["source"] == "workflow"


def test_unbound_input_returns_400(project):
    stage = read_stage(project, "load")
    stage["connector"]["params"] = {}
    add_stage(project, stage)
    save_working_copy_as_version(project.name, message="unbound")

    resp = client.post("/project/demo/run",
                       data={"binding__load": ""}, follow_redirects=False)
    assert resp.status_code == 400
    assert "load" in resp.json()["detail"]
    assert not (project / "runs").exists()


def test_new_run_page_shows_one_picker_per_file_input(project, tmp_path):
    _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)
    resp = client.get("/project/demo/runs/new")
    assert resp.status_code == 200
    assert 'name="binding__load"' in resp.text
    assert str(project / "a.csv") in resp.text   # the authored path, as the blank option
    assert "b.csv" in resp.text                  # and the project's stored file


def test_file_picker_lists_newest_upload_first_with_absolute_times(project, tmp_path):
    _store("older.csv", pd.DataFrame({"name": ["a"], "val": [1]}), tmp_path)
    _store("newer.csv", pd.DataFrame({"name": ["b"], "val": [2]}), tmp_path)
    records = {record.filename: record for record in list_project_files("demo")}
    records["older.csv"].created_at = "2026-07-02T09:05:00"
    records["older.csv"].save()
    records["newer.csv"].created_at = "2026-08-19T16:42:00"
    records["newer.csv"].save()

    body = client.get("/project/demo/runs/new").text

    newer = "Uploaded 19 Aug 2026, 16:42 · newer.csv · 13B"
    older = "Uploaded 2 Jul 2026, 09:05 · older.csv · 13B"
    assert body.index(newer) < body.index(older)
    assert 'data-uploaded-at="2026-08-19T16:42:00"' in body

    files = client.get("/project/demo/run-inputs").json()["files"]
    assert [file["filename"] for file in files] == ["newer.csv", "older.csv"]
    assert [file["label"] for file in files] == [newer, older]


def test_the_picker_row_says_what_the_record_knows(project, tmp_path):
    _store("stories.csv", pd.DataFrame({"name": ["a"], "val": [1]}), tmp_path)
    record = list_project_files("demo")[0]
    update_file_provenance("demo", record.id, FileCompleteness.SAMPLED,
                           "Every filing FOIA returned, minus the sealed ones.")

    body = client.get("/project/demo/runs/new").text

    assert "never read" in body
    assert "sampled" in body
    assert "minus the sealed ones" in body


def test_the_preview_dialog_is_the_way_to_the_files_own_page(project, tmp_path):
    _store("stories.csv", pd.DataFrame({"name": ["a"], "val": [1]}), tmp_path)
    body = client.get("/project/demo/runs/new").text
    # The row offers Preview alone; the dialog carries the link, filled in when it loads.
    assert 'data-picker-row-action="file-preview"' in body
    assert 'class="file-preview-page-link"' in body
    assert f'value="{list_project_files("demo")[0].id}"' in body


def test_file_picker_refuses_a_choice_without_an_upload_time():
    with pytest.raises(ValidationError, match="uploaded_at"):
        FileChoice.model_validate({"file_id": "abc", "filename": "a.csv", "bytes": 1})


def test_file_picker_refuses_an_invalid_stored_upload_time():
    record = ProjectFile(
        sha256="abc",
        filename="a.csv",
        byte_count=1,
        created_at="not-a-time",
    )

    with pytest.raises(ValueError, match="Invalid isoformat string"):
        build_file_choice(record)


def test_file_picker_renders_shared_structured_controls(project, tmp_path):
    _store("stories.csv", pd.DataFrame({"name": ["a"], "val": [1]}), tmp_path)

    body = client.get("/project/demo/runs/new").text

    assert 'class="picker" data-picker' in body
    assert 'class="picker-native file-pick"' in body
    assert 'data-picker-row-action="file-preview"' in body
    assert 'class="picker-trigger"' in body
    assert 'class="picker-popover" role="dialog"' in body
    assert 'class="picker-list" role="group"' in body
    assert 'data-name="stories.csv"' in body
    assert 'data-meta="Uploaded ' in body
    assert 'data-side="13B"' in body
    assert "Uploads newest first" in body
    assert 'class="picker-search" aria-label="Search files"' in body
    assert "Nothing matches this search." in body
    assert "/static/picker.css" in body
    assert "/static/picker.js" in body
    assert 'class="btn file-preview-open"' not in body


def test_required_file_picker_keeps_native_form_validation(project):
    stage = read_stage(project, "load")
    stage["connector"]["params"] = {}
    add_stage(project, stage)
    save_working_copy_as_version("demo", message="unbound")

    body = client.get("/project/demo/runs/new").text
    picker = body.split('class="picker" data-picker', 1)[1].split("</select>", 1)[0]

    assert 'name="binding__load"' in picker
    assert "required" in picker


def test_the_authored_path_is_what_the_picker_reads_while_nothing_is_bound(project):
    body = client.get("/project/demo/runs/new").text
    picker = body.split('class="picker" data-picker', 1)[1].split("</select>", 1)[0]

    # No blank option: nothing selected IS the blank.
    assert "multiple" in picker
    assert 'value=""' not in picker
    assert f'data-empty-name="{project / "a.csv"}"' in picker
    assert "required" not in picker


def test_binding_several_files_to_one_input_records_the_paths_it_read(project, tmp_path):
    ids = [_store("jun.csv", pd.DataFrame({"name": ["a"], "val": [1]}), tmp_path),
           _store("jul.csv", pd.DataFrame({"name": ["b"], "val": [2]}), tmp_path)]
    resp = client.post("/project/demo/run",
                       data={"binding__load": ids}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    record = _manifest(project)["input_bindings"]["load"]
    assert [Path(f["path"]).name for f in record["files"]] == ["jun.csv", "jul.csv"]
    assert record["source"] == "run"


def test_the_rows_of_every_bound_file_reach_the_stage_output(project, tmp_path):
    ids = [_store("jun.csv", pd.DataFrame({"name": ["a"], "val": [1]}), tmp_path),
           _store("jul.csv", pd.DataFrame({"name": ["b"], "val": [2]}), tmp_path)]
    client.post("/project/demo/run", data={"binding__load": ids}, follow_redirects=False)
    run_dir = sorted((project / "runs").iterdir())[-1]
    rows = read_frame_table(run_dir / "outputs" / "load.parquet").to_pylist()
    assert [row["name"] for row in rows] == ["a", "b"]


def test_shared_picker_wires_pointer_keyboard_and_dynamic_refresh():
    source = (Path(__file__).parents[1] / "app/static/picker.js").read_text(
        encoding="utf-8"
    )
    preview = (Path(__file__).parents[1] / "app/static/file-preview.js").read_text(
        encoding="utf-8"
    )

    for key in ("ArrowDown", "ArrowUp", "Home", "End", "Enter", "Escape"):
        assert f'event.key === "{key}"' in source or f'"{key}"' in source
    assert 'document.addEventListener("click"' in source
    assert "select.tabIndex = -1" in source
    assert 'select.setAttribute("aria-hidden", "true")' in source
    assert "api.open = function (picker) { openPopover(picker, 1); };" in source
    assert 'trigger.setAttribute("aria-controls", select.id + "__picker")' in source
    assert "row.appendChild(item)" in source
    assert "if (action) row.appendChild(action)" in source
    assert 'picker.addEventListener("focusout"' in source
    assert 'api.rowActions["file-preview"] = buildPreviewAction' in preview
    assert "button.dataset.fileId = option.value" in preview
    assert 'button.setAttribute("aria-haspopup", "dialog")' in preview
    assert 'button.setAttribute("aria-controls", "run-file-preview")' in preview
    assert 'button.setAttribute("aria-disabled", "true")' in preview
    # static/tooltip.js owns the node this opens, including its role.
    assert 'button.setAttribute("data-tip", "Only project files can be previewed.")' in preview


def test_row_preview_does_not_change_the_selected_file():
    preview_source = (Path(__file__).parents[1] / "app/static/file-preview.js").read_text(
        encoding="utf-8"
    )

    preview_action = preview_source.split("function buildPreviewAction", 1)[1].split(
        "function wirePreviewDialog", 1
    )[0]
    load_preview = preview_source.split("async function loadPreview", 1)[1].split(
        "form.addEventListener", 1
    )[0]
    assert "chooseOption" not in preview_action
    assert "select.value" not in load_preview
    assert "var fileId = button.dataset.fileId" in load_preview


def test_picker_visible_content_cannot_intercept_pointer_clicks():
    source = (Path(__file__).parents[1] / "app/static/picker.css").read_text(
        encoding="utf-8"
    )

    value_rule = source.split(".picker-value {", 1)[1].split("}", 1)[0]
    chevron_rule = source.split(".picker-chevron {", 1)[1].split("}", 1)[0]
    assert "pointer-events: none" in value_rule
    assert "user-select: none" in value_rule
    assert "pointer-events: none" in chevron_rule
    assert "user-select: none" in chevron_rule


# ─── The run form is its own page, not a block on the run history ────────────
# Configuring a run and reading the history are different tasks with different
# fields; "new" also has to reach run_new rather than being read as a run id by
# /runs/{run_id}, which is registered after it.

def test_runs_index_carries_no_run_form(project):
    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    assert 'class="run-controls"' not in resp.text
    assert 'name="binding__load"' not in resp.text
    assert '/project/demo/runs/new' in resp.text  # the action that reaches it


def test_runs_index_carries_no_awaiting_review_banner(project):
    assert "banner-review" not in client.get("/project/demo/runs").text


def test_the_zero_state_offers_a_button_not_a_link_in_a_sentence(project):
    body = client.get("/project/demo/runs").text
    zero = body.split('class="empty-state"')[1].split("</div>")[0]
    assert "No runs yet" in zero
    assert '<a href="/project/demo/runs/new" class="btn primary">Start new run</a>' in zero


def test_new_is_the_run_form_not_a_run_id(project):
    resp = client.get("/project/demo/runs/new")
    assert resp.status_code == 200
    assert 'action="/project/demo/run"' in resp.text


def test_new_run_page_labels_the_row_cap_separately(project):
    resp = client.get("/project/demo/runs/new")
    # It used to sit inside the row's label, where clicking "first"/"rows" focused
    # the read-only path field — the label's first control.
    assert 'class="run-limit"' in resp.text
    assert 'for="binding__load"' in resp.text  # the name line labels the path field


def test_new_run_page_has_one_shared_upload_dialog(project):
    body = client.get("/project/demo/runs/new").text
    assert body.count('class="run-upload-dialog"') == 1
    assert 'class="run-upload-drop"' in body
    assert 'class="run-upload-selection" hidden' in body
    assert 'class="run-upload-error" role="alert" hidden' in body
    assert 'class="btn primary run-upload-submit" disabled' in body
    assert 'class="file-input"' not in body


def test_new_run_page_has_one_shared_file_preview_dialog(project):
    body = client.get("/project/demo/runs/new").text
    assert body.count('class="file-preview-dialog"') == 1
    assert 'aria-labelledby="run-file-preview-title"' in body
    assert 'class="file-preview-body" aria-live="polite"' in body
    assert "/static/file-preview.js" in body


def test_each_run_input_row_opens_the_shared_upload_dialog(project):
    body = client.get("/project/demo/runs/new").text
    assert 'class="btn browse-btn">Upload file…</button>' in body
    assert body.count('class="run-upload-dialog"') == 1


def _corrupt_version_document_with_relative_path(project):
    from app.core.persistence import get_store
    from app.services.versioning import list_versions

    version_id = list_versions(project.name)[0].version_id
    store = get_store()
    doc = store.read("workflow_version", f"{project.name}/{version_id}")
    doc["stages"][0]["connector"]["params"]["paths"] = ["relative/a.csv"]
    store.write("workflow_version", f"{project.name}/{version_id}", doc)
    return version_id


# A version document that no longer validates (e.g. a legacy repo-relative path)
# fails LOUDLY on every read — listing included — as WorkflowLoadError. No page
# renders as if the store were healthy; the remedy for legacy documents is a
# store migration, never a silent skip. The trigger endpoint translates the
# failure into a structured 400 naming the issues.

def test_runs_page_fails_loudly_for_an_invalid_version(project):
    from app.services.loader import WorkflowLoadError

    _corrupt_version_document_with_relative_path(project)
    with pytest.raises(WorkflowLoadError, match="ABSOLUTE"):
        client.get("/project/demo/runs")


def test_trigger_run_returns_400_with_issues_for_an_invalid_version(project):
    _corrupt_version_document_with_relative_path(project)
    resp = client.post("/project/demo/run", data={}, follow_redirects=False)
    assert resp.status_code == 400
    assert any("ABSOLUTE" in issue for issue in resp.json()["issues"])


def test_loading_an_invalid_version_explicitly_surfaces_issues(project):
    from app.services.loader import WorkflowLoadError
    from app.services.versioning import load_version_stages

    version_id = _corrupt_version_document_with_relative_path(project)
    with pytest.raises(WorkflowLoadError) as exc:
        load_version_stages(project.name, version_id)
    assert any("ABSOLUTE" in issue for issue in exc.value.issues)
