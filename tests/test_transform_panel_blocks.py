"""The node-review Transform pane shows what the step actually computes, for
every transform config block — including the two that carry no `function` block:
filter_rows' predicate and union's declared inputs. A stage type whose config block
the panel doesn't render reads as an empty Transform pane, which is worse than
useless: it says "nothing happens here"."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

_SCHEMA = {"columns": [
    {"name": "client", "type": "str", "nullable": False},
    {"name": "relevance", "type": "str", "nullable": True},
]}

_PREDICATE = (
    "def should_include(row):\n"
    "    return row['relevance'] == 'incidental'\n"
)


def _seed_project(root: Path) -> None:
    compiled = root / "alpha" / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "output_schema": _SCHEMA,
    }), encoding="utf-8")
    (compiled / "02_load_more.json").write_text(json.dumps({
        "id": "load_more", "name": "Load more", "type": "input_data",
        "connector": {"kind": "file"}, "output_schema": _SCHEMA,
    }), encoding="utf-8")
    (compiled / "03_all_filings.json").write_text(json.dumps({
        "id": "all_filings", "name": "Every filing", "type": "union",
        "inputs": [{"id": "load", "schema": _SCHEMA},
                   {"id": "load_more", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
        "union": {},
    }), encoding="utf-8")
    (compiled / "04_incidental.json").write_text(json.dumps({
        "id": "select_incidental_filings", "name": "The incidental mentions",
        "type": "filter_rows",
        "inputs": [{"id": "all_filings", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
        "filter": {"code": _PREDICATE},
    }), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    import app.web.loading as loading
    import app.web.routers.node_review as node_review_router
    monkeypatch.setattr(node_review_router, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    _seed_project(tmp_path)
    return TestClient(app)


def test_filter_rows_panel_shows_the_predicate_source(client: TestClient) -> None:
    response = client.get("/project/alpha/node/select_incidental_filings/review-partial")
    assert response.status_code == 200
    html = response.text
    assert "Row filter" in html
    assert "should_include" in html
    assert "row[&#39;relevance&#39;] == &#39;incidental&#39;" in html


def test_union_panel_names_the_inputs_it_concatenates(client: TestClient) -> None:
    response = client.get("/project/alpha/node/all_filings/review-partial")
    assert response.status_code == 200
    html = response.text
    assert "Union" in html
    assert "load_more" in html
