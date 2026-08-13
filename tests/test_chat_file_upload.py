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
from app.services.uploads import UploadedFile, list_project_files
from app.tools import shared
from app.web.file_sizes import read_attachment

client = TestClient(app)

CSV = b"name,val\nx,1\n"
CSV_SHA = hashlib.sha256(CSV).hexdigest()


@pytest.fixture
def workspace_with_a_project(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    # A real project record, so the page's picker has something true to offer.
    return create_project("demo", "A methodology.", source="test").id


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
    assert [r.sha256 for r in list_project_files(None)] == [CSV_SHA]


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


def test_new_project_leads_the_choices(session_id):
    page = client.get(f"/chat/{session_id}").text
    # First and bold: with no project on the session it is the likeliest answer. Its
    # value is blank, so the file lands in no project and the agent creates one and
    # moves it in — nothing in the browser can, since a project needs a methodology.
    assert 'data-project="">\n      <strong>New project</strong>' in page
    assert page.index("ac-choice-new") < page.index('id="project-choices"')


def test_the_picker_names_the_project_it_offers(session_id, project_id):
    page = client.get(f"/chat/{session_id}").text
    # A generated id says nothing to a reader deciding where their file goes, so the
    # choice carries the name and the id rides along to identify it.
    assert '"name": "demo"' in page or '"name":"demo"' in page
    assert project_id in page


# ─── The agent's side: adopting a file that arrived before any project ───────

def test_the_agent_can_see_what_has_no_home(session_id):
    attach(session_id)
    assert [f.sha256 for f in shared.list_files(None, "http://x/files").files] == [CSV_SHA]


def test_adopting_gives_it_one_and_moves_no_bytes(session_id, project_id):
    attach(session_id)
    before = client.get(f"/chat/{session_id}")  # the page still renders mid-flight
    assert before.status_code == 200
    adopted = shared.move_file_to_project(project_id, CSV_SHA)
    assert adopted.filename == "posts.csv"
    assert list_project_files(None) == []
    assert [r.filename for r in list_project_files(project_id)] == ["posts.csv"]


def test_adopting_something_already_owned_fails_loudly(session_id, project_id):
    attach(session_id, project_id=project_id)
    with pytest.raises(FileNotStoredError, match="outside a project"):
        shared.move_file_to_project(project_id, CSV_SHA)


# ─── The turn draws as a card; its text is still the sentence the agent reads ────

def test_a_file_turn_draws_as_a_card(session_id, project_id):
    line = attach(session_id, project_id=project_id).json()["line"]
    client.post(f"/chat/{session_id}/message", json={"text": line})
    page = client.get(f"/chat/{session_id}").text
    assert 'class="ac-body ac-file"' in page
    assert '<span class="ac-file-name">posts.csv</span>' in page


def test_an_ordinary_message_is_still_its_own_text(session_id):
    client.post(f"/chat/{session_id}/message", json={"text": "just a message"})
    page = client.get(f"/chat/{session_id}").text
    assert 'class="ac-body ac-file"' not in page
    assert "just a message" in page


def test_the_card_changes_how_the_line_looks_and_never_what_it_says(session_id, project_id):
    line = attach(session_id, project_id=project_id).json()["line"]
    card = read_attachment(line)
    assert card is not None
    # Every field of the sentence survives the split, because the agent is given the
    # sentence and the reader is given the card, and they must not diverge.
    assert card.name == "posts.csv"
    # Only the size rides on the chip; the project and hash stay in the text the agent
    # reads, because on screen they turn a chip into a paragraph.
    assert card.meta == "13B"
    assert project_id in line and CSV_SHA in line
    assert read_attachment("just a message") is None
