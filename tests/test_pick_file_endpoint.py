"""POST /project/{name}/pick-file — the native macOS 'Choose File' dialog behind
the run form's Browse… button. The dialog is macOS GUI, so tests monkeypatch the
picker function and assert the endpoint's JSON contract; one test drives the
platform guard in pick_file_native directly (without spawning a dialog)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.web.loading as loading
import app.web.routers.runs as runs_router
from app.main import app
from app.web.loading import NativePickerUnavailable

client = TestClient(app)


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    (proj / "data").mkdir(parents=True)
    monkeypatch.setattr(runs_router, "EXAMPLES_DIR", tmp_path)
    return proj


def test_pick_returns_chosen_path(project, monkeypatch):
    monkeypatch.setattr(runs_router, "pick_file_native",
                        lambda start: "/data/posts.parquet")
    body = client.post("/project/demo/pick-file").json()
    assert body == {"ok": True, "cancelled": False, "path": "/data/posts.parquet"}


def test_dialog_opens_in_the_project_data_dir(project, monkeypatch):
    seen = {}
    monkeypatch.setattr(runs_router, "pick_file_native",
                        lambda start: seen.setdefault("start", start) and None)
    client.post("/project/demo/pick-file")
    assert seen["start"] == project / "data"  # opens where the data lives


def test_cancel_is_not_an_error(project, monkeypatch):
    monkeypatch.setattr(runs_router, "pick_file_native", lambda start: None)
    body = client.post("/project/demo/pick-file").json()
    assert body == {"ok": True, "cancelled": True, "path": None}


def test_unavailable_dialog_returns_501(project, monkeypatch):
    def boom(start):
        raise NativePickerUnavailable("native file dialog is macOS-only")
    monkeypatch.setattr(runs_router, "pick_file_native", boom)
    resp = client.post("/project/demo/pick-file")
    assert resp.status_code == 501
    assert resp.json()["ok"] is False and "macOS" in resp.json()["error"]


def test_unknown_project_404(project):
    assert client.post("/project/nope/pick-file").status_code == 404


def test_pick_file_native_refuses_non_macos(monkeypatch):
    # The platform guard fails fast — no osascript, no dialog — off macOS.
    monkeypatch.setattr(loading.sys, "platform", "linux")
    with pytest.raises(NativePickerUnavailable, match="macOS-only"):
        loading.pick_file_native(None)
