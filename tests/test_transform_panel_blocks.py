"""The node-review Transform pane shows what the step actually computes, for
every transform config block — including the two that carry no `function` block:
filter_rows' predicate and union's declared inputs. A stage type whose config block
the panel doesn't render reads as an empty Transform pane, which is worse than
useless: it says "nothing happens here"."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from stage_seed import add_stage

_SCHEMA = {"columns": [
    {"name": "client", "type": "str", "nullable": False},
    {"name": "relevance", "type": "str", "nullable": True},
]}

_PREDICATE = (
    "def should_include(row):\n"
    "    return row['relevance'] == 'incidental'\n"
)


def _seed_project(root: Path) -> None:
    compiled = root / "alpha"
    compiled.mkdir(parents=True, exist_ok=True)
    add_stage(compiled, {
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _SCHEMA["columns"]},
    })
    add_stage(compiled, {
        "id": "load_more", "description": "Load more", "type": "input_data",
        "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _SCHEMA["columns"]},
    })
    add_stage(compiled, {
        "id": "all_filings", "description": "Every filing", "type": "union",
        "inputs": [{"id": "load"},
                   {"id": "load_more"}],
        "signature": {"form": "extends", "reads": [], "adds": [], "rewrites": []},
        "union": {},
    })
    add_stage(compiled, {
        "id": "select_incidental_filings", "description": "The incidental mentions",
        "type": "filter_rows",
        "inputs": [{"id": "all_filings"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "all_filings", "columns": _SCHEMA["columns"]}]},
        "filter": {"code": _PREDICATE},
    })


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    workspace.set_projects_dir(tmp_path)
    _seed_project(tmp_path)
    return TestClient(app)


def test_filter_rows_panel_shows_the_predicate_source(client: TestClient) -> None:
    response = client.get("/project/alpha/node/select_incidental_filings/panel")
    assert response.status_code == 200
    html = response.text
    assert "Row filter" in html
    assert "should_include" in html
    assert "row[&#39;relevance&#39;] == &#39;incidental&#39;" in html


def test_union_panel_names_the_inputs_it_concatenates(client: TestClient) -> None:
    response = client.get("/project/alpha/node/all_filings/panel")
    assert response.status_code == 200
    html = response.text
    assert "Union" in html
    assert "load_more" in html
