"""Spends real tokens. Deselected by default (pytest.ini adds -m "not live_llm");
opt in with `pytest -m live_llm`. Bounded by construction to 3 model calls per
run. With no backend this tier FAILS rather than skips.
"""
from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import create_project
from stage_seed import set_stages
from test_journey_smoke import _point_examples_dir_at, assert_run_ok

client = TestClient(app)

pytestmark = pytest.mark.live_llm

PROJECT = "smoke_live_llm"
ROWS = 1         # one CSV row = one model call per attempt
MAX_RETRIES = 2  # transient-failure retries per row (attempts = retries + 1)


@pytest.fixture
def offline_llm():
    """Shadows conftest's autouse offline fixture so this tier hits the real backend."""
    yield


def test_the_offline_seal_lets_this_tier_reach_the_real_sdk() -> None:
    # Costs nothing, and fails here rather than 3 model calls deep if the seal widens.
    import app.core.agent.sdk_engine as sdk_engine
    from claude_agent_sdk import query

    assert sdk_engine.query is query


def test_live_llm_journey_reaches_a_published_artifact(live_project):
    from app.runtime.options import agent_available
    assert agent_available(), (
        "live_llm tier needs claude-agent-sdk + the claude CLI. Install both "
        "(and set ANTHROPIC_API_KEY in CI) — this tier fails rather than skips."
    )
    project = live_project.name

    resp = client.post(f"/project/{project}/version", data={"message": "live smoke"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True, resp.text

    resp = client.post(f"/project/{project}/run", data={}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    run_id = resp.headers["location"].rstrip("/").rsplit("/", 1)[-1]

    status = client.get(f"/project/{project}/runs/{run_id}/status").json()
    assert_run_ok(status, live_project, run_id)

    # The live signal: a real reply arrived for EVERY row. The column is
    # nullable, so a blanked row would still leave the run `ok` — count the
    # answers, don't trust the status. Values are the model's judgment and are
    # deliberately not asserted; conformance (present, boolean) is the contract.
    served = client.get(f"/project/{project}/runs/{run_id}/artifact/report/classified.csv")
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
    _point_examples_dir_at(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background", lambda target, *args: target(*args))

    source = tmp_path / "claims.csv"
    frame = pd.DataFrame({
        "claim_id": ["c1"],
        "text": ["The company paid $2 million to the lobbying firm."],
    })
    assert len(frame) == ROWS  # budget bound: one model call per row per attempt
    frame.to_csv(source, index=False)

    project_id = create_project(PROJECT, "Classify claims and publish the table.",
                                source="live smoke test").id
    project_dir = tmp_path / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    set_stages(project_id, _workflow_stages(str(source)))
    return project_dir


def _workflow_stages(source_path: str) -> list[dict]:
    load_schema = {
        "columns": [{"name": "claim_id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True}],
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
        "id": "classify", "description": "Classify claims", "type": "llm_transform",
        "inputs": [{"id": "load"}],
        # Reads must match the template's placeholders exactly: it injects {text}.
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [
                {"name": "text", "type": "str", "nullable": True}]}],
            "adds": [{"name": "about_money", "type": "bool", "nullable": True}],
        },
        "llm": llm,
    }
    return [
        {
            "id": "load", "description": "Load claims", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": source_path, "format": "csv"}},
            "signature": {"form": "replaces", "produces": load_schema["columns"]},
        },
        classify,
        {
            "id": "report", "description": "Publish classified claims", "type": "report",
            "inputs": [{"id": "classify"}],
            "report": {"format": "csv", "destination": "report/"},
            "signature": {"form": "replaces"},
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
