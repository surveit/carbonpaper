"""Tier-2 smoke: the journey with a REAL llm_transform stage — spends tokens.

Deselected from a default local pytest run (pytest.ini adds `-m "not
live_llm"`) so `pytest` never spends tokens by surprise; opt in with
`pytest -m live_llm`. CI opts in on every push (the live-llm-smoke workflow),
which the tiny budget below is sized for.

Covers what tests/test_journey_smoke.py structurally cannot: real completions
arriving through the structured-output agent and conforming to the stage's
reply spec, transient-failure retries, and the timeout path.

Token budget is bounded BY CONSTRUCTION, and the bounds are asserted so they
survive future edits: 1 row x (max_retries=2 + 1) = at most 3 model calls per
run, on the default (small) model with a one-line prompt.

When the backend is missing this tier FAILS, never skips: opting in states an
intent to test the live path, and a silent skip would report it as covered.
"""
from __future__ import annotations

import io
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.routers.runs as runs_router
from app.main import app
from app.services.project import create_project
from app.services.versioning import list_versions
from test_journey_smoke import _point_examples_dir_at, assert_run_ok

client = TestClient(app)

pytestmark = pytest.mark.live_llm

PROJECT = "smoke_live_llm"
ROWS = 1         # one CSV row = one model call per attempt
MAX_RETRIES = 2  # transient-failure retries per row (attempts = retries + 1)


@pytest.fixture
def offline_llm():
    """Override conftest's autouse offline forcing: this tier runs the real
    backend. (Same fixture name shadows the session-wide monkeypatch.)"""
    yield


def test_live_llm_journey_reaches_a_published_artifact(live_project, tmp_path):
    from app.runtime.options import agent_available
    assert agent_available(), (
        "live_llm tier needs claude-agent-sdk + the claude CLI. Install both "
        "(and set ANTHROPIC_API_KEY in CI) — this tier fails rather than skips."
    )

    resp = client.post(f"/project/{PROJECT}/version", data={"message": "live smoke"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True, resp.text

    # Publish it — a run pins a PUBLISHED version, so the human-approval step is
    # part of the journey: author -> version -> publish -> run -> artifact.
    version_id = list_versions(tmp_path / PROJECT)[0].version_id
    resp = client.post(f"/project/{PROJECT}/versions/{version_id}/publish",
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text

    resp = client.post(f"/project/{PROJECT}/run", data={}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    run_id = resp.headers["location"].rstrip("/").rsplit("/", 1)[-1]

    status = client.get(f"/project/{PROJECT}/runs/{run_id}/status").json()
    assert_run_ok(status, live_project, run_id)

    # The live signal: a real reply arrived for EVERY row. The column is
    # nullable, so a blanked row would still leave the run `ok` — count the
    # answers, don't trust the status. Values are the model's judgment and are
    # deliberately not asserted; conformance (present, boolean) is the contract.
    served = client.get(f"/project/{PROJECT}/runs/{run_id}/artifact/report/classified.csv")
    assert served.status_code == 200
    table = pd.read_csv(io.StringIO(served.text))
    assert len(table) == ROWS
    assert table["about_money"].notna().all(), (
        f"model reply missing for some rows:\n{table}"
    )
    assert table["about_money"].map(lambda v: v in (True, False)).all()


# ── Fixture: journey project with one llm_transform ──────────────────────────

@pytest.fixture
def live_project(tmp_path, monkeypatch):
    _point_examples_dir_at(monkeypatch, tmp_path)
    monkeypatch.setattr(runs_router, "run_in_background", lambda target, *args: target(*args))

    source = tmp_path / "claims.csv"
    frame = pd.DataFrame({
        "claim_id": ["c1"],
        "text": ["The company paid $2 million to the lobbying firm."],
    })
    assert len(frame) == ROWS  # budget bound: one model call per row per attempt
    frame.to_csv(source, index=False)

    create_project(PROJECT, "Classify claims and publish the table.", source="live smoke test")
    project_dir = tmp_path / PROJECT
    (project_dir / "compiled").mkdir()
    for position, stage in enumerate(_workflow_stages(str(source)), start=1):
        path = project_dir / "compiled" / f"{position:02d}_{stage['id']}.json"
        path.write_text(json.dumps(stage), encoding="utf-8")
    return project_dir


def _workflow_stages(source_path: str) -> list[dict]:
    load_schema = {
        "columns": [{"name": "claim_id", "type": "str"}, {"name": "text", "type": "str"}],
        "primary_key": ["claim_id"],
    }
    classified_schema = {
        "columns": [{"name": "claim_id", "type": "str"}, {"name": "text", "type": "str"},
                    {"name": "about_money", "type": "bool", "nullable": True}],
        "primary_key": ["claim_id"],
    }
    llm = {
        "prompt_template": (
            'Statement: "{text}"\n'
            "Is this statement about money, payment, or revenue?"
        ),
        "max_retries": MAX_RETRIES,
    }
    assert llm["max_retries"] == MAX_RETRIES  # budget bound survives edits
    classify = {
        "id": "classify", "name": "Classify claims", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": load_schema}],
        "output_schema": classified_schema,
        "llm": llm,
    }
    return [
        {
            "id": "load", "name": "Load claims", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": source_path, "format": "csv"}},
            "output_schema": load_schema,
        },
        classify,
        {
            "id": "report", "name": "Publish classified claims", "type": "publish",
            "inputs": [{"id": "classify", "schema": classified_schema}],
            "publish": {"format": "csv", "destination": "report/"},
            "function": {"kind": "inline", "code": (
                "import pandas as pd\n"
                "from pathlib import Path\n"
                "\n"
                "def transform(df, output_dir):\n"
                "    out = Path(output_dir) / \"classified.csv\"\n"
                "    df.to_csv(out, index=False)\n"
                "    return pd.DataFrame({\"artifact\": [str(out)]})\n"
            )},
        },
    ]
