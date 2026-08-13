"""The open-demo gate must reach the browser CLOSED.

A <dialog open> in the shell is a blocking overlay on every page that only
demo-notice.js can dismiss, so shipping one with JavaScript off locks the app instead
of warning about it.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace

client = TestClient(app)


@pytest.fixture(autouse=True)
def empty_workspace(tmp_path):
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def test_the_gate_is_served_closed() -> None:
    html = client.get("/").text
    gate = re.search(r"<dialog[^>]*\bclass=\"demo-gate\"[^>]*>", html)
    assert gate is not None, "the shell serves no demo gate"
    assert " open" not in gate.group(0)


def test_the_strip_is_on_a_page_that_is_not_the_home_page() -> None:
    assert 'class="demo-strip"' in client.get("/chat").text
