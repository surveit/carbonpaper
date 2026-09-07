"""What the session-scoped draft buys: two chats editing one project in isolation."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import drafts, versioning
from app.tools.editing import EditingContext, _draft_of

_SESSION_A = "a" * 32
_SESSION_B = "b" * 32

_STAGE = {
    "id": "load", "type": "input_data", "description": "Load the rows",
    "connector": {"kind": "file"},
    "signature": {"form": "replaces",
                  "produces": [{"name": "a", "type": "str", "nullable": False}]},
}


@pytest.fixture()
def project(tmp_path: Path) -> str:
    versioning.create_version_from_stages(tmp_path.name, [_STAGE], message="v1")
    return tmp_path.name


def test_each_session_starts_from_the_newest_version(project: str) -> None:
    assert sorted(drafts.read_draft_specs(project, _SESSION_A)) == ["load"]
    assert sorted(drafts.read_draft_specs(project, _SESSION_B)) == ["load"]


def test_one_session_emptying_its_draft_leaves_the_other_alone(project: str) -> None:
    drafts.read_draft_specs(project, _SESSION_B)

    drafts.write_draft_specs(project, _SESSION_A, [])

    assert drafts.read_draft_specs(project, _SESSION_A) == {}
    assert sorted(drafts.read_draft_specs(project, _SESSION_B)) == ["load"]


def test_a_chat_names_its_draft_by_its_session(project: str) -> None:
    assert _draft_of(EditingContext(base_url="http://t/", session_id=_SESSION_A)) == _SESSION_A


def test_a_chat_with_no_session_has_no_draft_to_edit(project: str) -> None:
    with pytest.raises(ValueError, match="no session"):
        _draft_of(EditingContext(base_url="http://t/"))
