"""The open-demo notice renders on the Fly deploy and nowhere else.

A <dialog open> in the shell is a blocking overlay only demo-notice.js can dismiss, so
serving one with JavaScript off would lock the app rather than warn about it.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace

client = TestClient(app)


@pytest.fixture(autouse=True)
def empty_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    workspace.set_projects_dir(tmp_path)
    return tmp_path


@pytest.fixture
def on_fly(monkeypatch):
    monkeypatch.setenv("FLY_APP_NAME", "carbonpaper")


def test_nothing_is_said_off_fly() -> None:
    html = client.get("/").text
    assert "demo-strip" not in html
    assert "demo-gate" not in html


def test_the_strip_and_the_gate_are_on_every_page_of_the_deploy(on_fly) -> None:
    for path in ("/", "/chat"):
        html = client.get(path).text
        assert 'class="demo-strip"' in html, path
        assert 'class="demo-gate"' in html, path


def test_the_gate_is_served_closed(on_fly) -> None:
    gate = re.search(r"<dialog[^>]*\bclass=\"demo-gate\"[^>]*>", client.get("/").text)
    assert gate is not None, "the deploy serves no demo gate"
    assert " open" not in gate.group(0)
