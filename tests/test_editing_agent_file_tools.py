"""The editing agent's file tools — the in-app surface can see a file, not just be told
about one. The MCP server had these; this agent had no file tools at all."""
from __future__ import annotations

import io

import pytest

from app.services import workspace
from app.services.uploads import save_upload
from app.tools.editing import EditingContext, make_editing_tools

FILINGS = (
    b"client,registrant,income\n"
    b"COMCAST CORPORATION,CORNERSTONE GOVERNMENT AFFAIRS,40000.00\n"
    b"AMERICAN HOSPITAL ASSOCIATION,CORNERSTONE GOVERNMENT AFFAIRS,20000.00\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "demo").mkdir(parents=True)
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    return tmp_path / "demo"


def tools(project_id: str | None = "demo"):
    return {t.name: t for t in make_editing_tools(EditingContext(project_id=project_id))}


def test_the_agent_offers_the_file_tools(project):
    offered = tools()
    for name in ["list_files", "profile_file", "survey_workbook", "move_file_to_project"]:
        assert name in offered, f"{name} is not bound"


def test_it_reads_what_an_attached_file_holds(project):
    """The chat's attach line hands over a sha256 the agent could not previously use."""
    sha = save_upload("lda.csv", io.BytesIO(FILINGS), "demo").sha256
    profile = tools()["profile_file"].fn("demo", sha)
    assert [c.column for c in profile.columns] == ["client", "registrant", "income"]
    assert profile.row_count == 2


def test_the_upload_url_is_root_relative_here(project):
    """Absolute would be a guess at the address they reached the app on."""
    view = tools()["list_files"].fn("demo")
    assert view.file_upload_url == "/project/demo/files"


def test_listing_the_files_in_no_project_needs_no_project(project):
    save_upload("loose.csv", io.BytesIO(FILINGS), None)
    view = tools()["list_files"].fn()
    assert [f.filename for f in view.files] == ["loose.csv"]
    assert view.file_upload_url == "/files"


def test_a_file_in_no_project_can_be_moved_in_and_then_read(project):
    """The path an attachment takes when the chat's "New project" choice was used."""
    sha = save_upload("loose.csv", io.BytesIO(FILINGS), None).sha256
    tools()["move_file_to_project"].fn("demo", sha)
    assert tools()["profile_file"].fn("demo", sha).row_count == 2
