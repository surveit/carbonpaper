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

_MENTIONS = {"name": "mentions", "type": "list[str]", "nullable": True}

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
    add_stage(compiled, {
        "id": "one_filing_per_client", "description": "One filing per client",
        "type": "dedupe",
        "inputs": [{"id": "select_incidental_filings"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "select_incidental_filings",
                                 "columns": _SCHEMA["columns"]}]},
        "dedupe": {"keys": ["client"], "keep": "highest", "by": "relevance"},
    })
    add_stage(compiled, {
        "id": "one_row_per_mention", "description": "A row per mention",
        "type": "explode",
        "inputs": [{"id": "one_filing_per_client"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "one_filing_per_client",
                                 "columns": [_MENTIONS]}],
                      "rewrites": [{"name": "mentions", "type": "str", "nullable": True}]},
        "explode": {"column": "mentions", "keep_empty": True},
    })
    add_stage(compiled, {
        "id": "worst_first", "description": "Worst first",
        "type": "sort_rank",
        "inputs": [{"id": "select_incidental_filings"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "select_incidental_filings",
                                 "columns": _SCHEMA["columns"]}],
                      "adds": [{"name": "rank", "type": "int", "nullable": False}]},
        "sort_rank": {"keys": [{"column": "relevance",
                                "order": ["central", "incidental"]},
                               {"column": "client", "descending": True}],
                      "rank_column": "rank"},
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
    assert "Keeps the rows this predicate returns" in html
    assert "should_include" in html
    assert "row[&#39;relevance&#39;] == &#39;incidental&#39;" in html


def test_union_panel_names_the_inputs_it_concatenates(client: TestClient) -> None:
    response = client.get("/project/alpha/node/all_filings/panel")
    assert response.status_code == 200
    html = response.text
    assert "Union" in html
    assert "load_more" in html


def test_dedupe_panel_names_its_keys_and_which_row_survives(client: TestClient) -> None:
    response = client.get("/project/alpha/node/one_filing_per_client/panel")
    assert response.status_code == 200
    html = response.text
    assert "Dedupe" in html
    assert "<code>client</code>" in html
    assert "The row with the highest <code>relevance</code>" in html


def test_explode_panel_names_the_column_and_what_an_empty_list_does(
        client: TestClient) -> None:
    response = client.get("/project/alpha/node/one_row_per_mention/panel")
    assert response.status_code == 200
    html = response.text
    assert "Explode" in html
    assert "<code>mentions</code>" in html
    assert "set to null" in html


def test_sort_rank_panel_lists_its_keys_in_priority_order(client: TestClient) -> None:
    response = client.get("/project/alpha/node/worst_first/panel")
    assert response.status_code == 200
    html = response.text
    assert "Sort and rank" in html
    assert html.index("<code>relevance</code>") < html.index("<code>client</code>")
    assert "largest first" in html
    assert "<code>rank</code>" in html
