"""The Transform pane reads description → examples → code for an authored-code
handle: python is the one transform a non-engineer reviewer cannot read, so the
plain-language `summary` leads and the source is folded away last. A stage with
no summary says so rather than silently leading with code."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import parse_stage
from app.services import workspace

_IN_SCHEMA = {"columns": [
    {"name": "bill_id", "type": "str", "nullable": False},
    {"name": "status", "type": "str", "nullable": False},
]}
_OUT_SCHEMA = {"columns": [
    {"name": "bill_id", "type": "str", "nullable": False},
    {"name": "status", "type": "str", "nullable": False},
    {"name": "withdrawn", "type": "bool", "nullable": False},
]}

_CODE = (
    "def transform(row):\n"
    "    return {**row, 'withdrawn': 'withdrawn' in row['status'].lower()}\n"
)
_SUMMARY = "Marks a bill as withdrawn when its status text says so."


def _seed_project(root: Path) -> None:
    compiled = root / "alpha" / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]},
    }), encoding="utf-8")
    (compiled / "02_flag.json").write_text(json.dumps({
        "id": "flag_withdrawn", "description": "Flag withdrawn bills",
        "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
            "adds": [{"name": "withdrawn", "type": "bool", "nullable": False}],
        },
        "function": {"kind": "inline", "summary": _SUMMARY, "code": _CODE},
        "tests": [{
            "name": "withdrawn_status_sets_the_flag",
            "inputs": {"load": [{"bill_id": "HB1", "status": "Withdrawn"}]},
            "expected": [{"bill_id": "HB1", "status": "Withdrawn", "withdrawn": True}],
        }],
    }), encoding="utf-8")
    (compiled / "03_unsummarized.json").write_text(json.dumps({
        "id": "no_summary", "description": "Unsummarized step",
        "type": "python_row_function",
        "inputs": [{"id": "flag_withdrawn", "schema": _OUT_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "flag_withdrawn", "columns": _OUT_SCHEMA["columns"]}],
        },
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    }), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    workspace.set_projects_dir(tmp_path)
    _seed_project(tmp_path)
    return TestClient(app)


def test_the_panel_reads_summary_then_examples_then_code(client: TestClient) -> None:
    html = client.get("/project/alpha/node/flag_withdrawn/panel").text
    summary_at = html.index(_SUMMARY)
    tests_at = html.index("withdrawn_status_sets_the_flag")
    code_at = html.index("def transform(row)")
    assert summary_at < tests_at < code_at


def test_a_stage_without_a_summary_says_so(client: TestClient) -> None:
    html = client.get("/project/alpha/node/no_summary/panel").text
    assert "No plain-language summary" in html


def test_a_summary_does_not_change_what_the_stage_computes() -> None:
    # `function` is its own name because mypy cannot `**`-spread a heterogeneous dict literal.
    function = {"kind": "inline", "summary": _SUMMARY, "code": _CODE}
    spec = {
        "id": "flag", "description": "Flag", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
            "adds": [{"name": "withdrawn", "type": "bool", "nullable": False}],
        },
        "function": function,
    }
    with_summary = parse_stage(spec)
    reworded = parse_stage({**spec, "function": {
        **function, "summary": "Totally different wording."}})
    assert (with_summary.compute_definition_fingerprint()
            == reworded.compute_definition_fingerprint())
