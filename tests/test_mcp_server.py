"""The MCP authoring surface: /mcp endpoint wiring + tool behavior.

Endpoint tests drive the real streamable-HTTP mount through TestClient (the app
lifespan starts the MCP session manager — hence the module-scoped `with` client:
the session manager's run() is once-per-process). Tool-behavior tests call the
tool functions directly against a tmp workspace."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@pytest.fixture(scope="module")
def client():
    from app.main import app

    # FastMCP auto-enables DNS-rebinding protection for its default host
    # (127.0.0.1), which only allow-lists Host headers of the form
    # "127.0.0.1:<port>" / "localhost:<port>" — TestClient's default
    # "testserver" Host fails that check with 421. Point the client at an
    # allowed host instead of loosening the server's security settings.
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


def test_mcp_endpoint_initializes(client):
    resp = client.post("/mcp", json=INITIALIZE, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"] == "glassbox"
    assert resp.history == []  # exact-path match — a 307 redirect would break non-following MCP clients


def test_mcp_lists_the_authoring_tools(client):
    client.post("/mcp", json=INITIALIZED, headers=HEADERS)
    resp = client.post("/mcp", json=LIST_TOOLS, headers=HEADERS)
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert {
        "list_projects",
        "create_project",
        "get_project_status",
        "generate_data_model",
        "generate_workflow",
        "read_data_model",
        "describe_workflow",
        "read_stage",
        "edit_stage",
        "add_stage",
    } <= names


def test_create_project_tool_and_status(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import workspace

    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    created = server.create_project(name="Money Trail", document="Follow the filings.")
    assert created["project_id"] == "money_trail"
    status = server.get_project_status(project_id="money_trail")
    assert status["has_document"] is True


def test_generate_data_model_kicks_the_live_turn(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import workspace

    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    server.create_project(name="probe", document="doc text")

    seen: dict[str, object] = {}

    def fake_start(pdir: Path, *, document: str, model: str) -> str:
        seen["pdir"] = pdir
        seen["document"] = document
        return "sess123"

    monkeypatch.setattr(server.generation, "start_generation", fake_start)
    out = asyncio.run(server.generate_data_model(project_id="probe"))
    assert out["watch"] == "/chat/sess123"
    assert seen["document"] == "doc text"


def test_generate_data_model_without_document_fails_loudly(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import workspace

    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    (tmp_path / "empty_proj").mkdir()
    with pytest.raises(ValueError):
        asyncio.run(server.generate_data_model(project_id="empty_proj"))


def test_read_tools_reject_unknown_project(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import workspace

    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    with pytest.raises(ValueError):
        server.read_data_model(project_id="no_such_project")
    with pytest.raises(ValueError):
        server.describe_workflow(project_id="no_such_project")
