"""Attaching a data file to a conversation. The bytes go to the store over HTTP and
never through a message; the turn carries one line naming the file — the same line the
reader sees and the agent is told, so the two cannot disagree."""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.services.errors import FileNotStoredError
from app.services.project import create_project
from app.services.uploads import UploadedFile, list_project_files, list_unclaimed_files
from app.tools import shared

client = TestClient(app)

CSV = b"name,val\nx,1\n"
CSV_SHA = hashlib.sha256(CSV).hexdigest()


@pytest.fixture
def workspace_with_a_project(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    # A real project record, so the page's picker has something true to offer.
    return create_project("demo", "A methodology.", source="test")


@pytest.fixture
def project_id(workspace_with_a_project) -> str:
    return workspace_with_a_project


@pytest.fixture
def session_id(workspace_with_a_project) -> str:
    resp = client.post("/chat/new", follow_redirects=False)
    return resp.headers["location"].rsplit("/", 1)[-1]


def attach(sid: str, project_id: str = "", name: str = "posts.csv", body: bytes = CSV):
    return client.post(f"/chat/{sid}/files",
                       files={"file": (name, body, "text/csv")},
                       data={"project_id": project_id})


def test_a_file_named_for_a_project_is_claimed_by_it(session_id, project_id):
    body = attach(session_id, project_id=project_id).json()
    assert body["ok"] is True
    assert body["project_id"] == project_id
    assert [r.filename for r in list_project_files(project_id)] == ["posts.csv"]


def test_a_file_with_no_project_stays_unclaimed(session_id):
    body = attach(session_id).json()
    assert body["project_id"] is None
    assert [r.sha256 for r in list_unclaimed_files()] == [CSV_SHA]


def test_the_line_says_where_the_file_went(session_id, project_id):
    claimed = attach(session_id, project_id=project_id).json()["line"]
    # The name is for whoever reads the conversation; the id is what run_workflow takes,
    # so the line carries both rather than making one of them guess.
    assert claimed == (f"[file] posts.csv · 13B · in project demo ({project_id}) · "
                       f"sha256 {CSV_SHA}")


def test_the_line_says_when_it_went_nowhere(session_id):
    assert "not in a project yet" in attach(session_id).json()["line"]


def test_the_line_carries_the_sha_run_workflow_binds(session_id, project_id):
    assert CSV_SHA in attach(session_id, project_id=project_id).json()["line"]
    # The agent reads this text and nothing else about the file, so what it needs to
    # start a run has to be in the sentence.


def test_an_oversized_file_is_refused_and_stored_nowhere(session_id, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_MAX_UPLOAD_BYTES", "8")
    resp = attach(session_id, body=b"x" * 9)
    assert resp.status_code == 400
    assert "over the 8B limit" in resp.json()["error"]
    assert UploadedFile.list() == []


def test_an_unknown_session_404s(workspace_with_a_project):
    assert attach("nosuchsession").status_code == 404


def test_the_page_carries_what_an_attachment_needs(session_id, project_id):
    page = client.get(f"/chat/{session_id}").text
    assert 'id="clip"' in page          # the attach control
    assert 'id="project-modal"' in page  # and the question, for a session with no project


def test_the_question_blocks_rather_than_sitting_beside_the_composer(session_id):
    page = client.get(f"/chat/{session_id}").text
    # A panel above the composer is answerable by ignoring it, and this decides where a
    # file lands. <dialog> brings the focus trap and Escape with it.
    assert '<dialog class="ac-modal" id="project-modal">' in page


def test_the_third_choice_is_a_new_project_not_a_shrug(session_id):
    page = client.get(f"/chat/{session_id}").text
    # Its value is blank, so the file lands unclaimed and the agent creates the project
    # and adopts it — nothing in the browser can, since a project needs a methodology.
    assert 'class="ac-choice ac-choice-new" data-project="">New project' in page


def test_the_picker_names_the_project_it_offers(session_id, project_id):
    page = client.get(f"/chat/{session_id}").text
    # A generated id says nothing to a reader deciding where their file goes, so the
    # choice carries the name and the id rides along to identify it.
    assert '"name": "demo"' in page or '"name":"demo"' in page
    assert project_id in page


# ─── The agent's side: adopting a file that arrived before any project ───────

def test_the_agent_can_see_what_has_no_home(session_id):
    attach(session_id)
    assert [f.sha256 for f in shared.list_unclaimed_files()] == [CSV_SHA]


def test_adopting_gives_it_one_and_moves_no_bytes(session_id, project_id):
    attach(session_id)
    before = client.get(f"/chat/{session_id}")  # the page still renders mid-flight
    assert before.status_code == 200
    adopted = shared.adopt_file(project_id, CSV_SHA)
    assert adopted.filename == "posts.csv"
    assert list_unclaimed_files() == []
    assert [r.filename for r in list_project_files(project_id)] == ["posts.csv"]


def test_adopting_something_already_owned_fails_loudly(session_id, project_id):
    attach(session_id, project_id=project_id)
    with pytest.raises(FileNotStoredError, match="no unclaimed file"):
        shared.adopt_file(project_id, CSV_SHA)
