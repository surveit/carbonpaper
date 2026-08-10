"""The /chat index's New chat control: the editing agent's context requires a
project_id, so the control names a project or says none exists — never a button
that would open a session about nothing.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.project import create_project

client = TestClient(app)


def test_new_chat_names_a_project_to_open_the_session_about() -> None:
    name = create_project("trail", "Follow the filings.", source="test")
    body = client.get("/chat").text
    assert f'<option value="{name}">' in body
    assert "/edit-agent" in body


def test_with_no_projects_the_control_says_so_instead_of_offering_a_chat() -> None:
    body = client.get("/chat").text
    assert "No projects yet" in body
    assert "/edit-agent" not in body
