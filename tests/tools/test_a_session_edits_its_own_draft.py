"""What the session-scoped store buys: two chats editing one project in isolation."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import stage_edit, versioning
from app.tools.editing import EditingContext, _open_stages

_SESSION_A = "a" * 32
_SESSION_B = "b" * 32

_STAGE = {
    "id": "load", "type": "input_data", "description": "Load the rows",
    "connector": {"kind": "file"},
    "signature": {"form": "replaces", "produces": [{"name": "a", "type": "str", "nullable": False}]},
}


def _context(session_id: str | None) -> EditingContext:
    return EditingContext(base_url="http://testserver/", session_id=session_id)


@pytest.fixture()
def project(tmp_path: Path) -> str:
    versioning.create_version_from_stages(tmp_path.name, [_STAGE], message="v1")
    return tmp_path.name


def test_each_session_starts_from_the_newest_version(project: str) -> None:
    a = _open_stages(project, _context(_SESSION_A))
    b = _open_stages(project, _context(_SESSION_B))

    assert sorted(a.read()) == ["load"]
    assert sorted(b.read()) == ["load"]


def test_one_session_emptying_its_draft_leaves_the_other_alone(project: str) -> None:
    a = _open_stages(project, _context(_SESSION_A))
    b = _open_stages(project, _context(_SESSION_B))

    a.write([])

    assert a.read() == {}
    assert sorted(b.read()) == ["load"]


def test_neither_session_touches_the_working_copy(project: str) -> None:
    a = _open_stages(project, _context(_SESSION_A))

    a.write([])

    assert stage_edit.open_working_copy(project).read() == {}


def test_a_chat_with_no_session_writes_the_working_copy_and_is_guarded(project: str) -> None:
    with pytest.raises(ValueError, match="no project"):
        _open_stages(project, _context(None))
