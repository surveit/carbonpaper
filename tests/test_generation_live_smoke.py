"""Tier-3 smoke: LIVE workflow generation from a tiny methodology — spends tokens.

Deselected from a default pytest run (pytest.ini); opt in with
`pytest -m live_generation`. CI runs it nightly + on manual dispatch (the
generation-smoke workflow) — not per-push: a generation turn takes minutes and
costs real money, and its job is catching GENERATOR DRIFT, which moves at the
pace of compiler-prompt/model changes, not per-commit.

The drift class this gates (all seen in the wild, palm_oil_mill_osint
2026-07-20): the generator emitting stages the current model rejects —
relative connector paths, removed connector kinds, double-braced prompt
columns. A generated workflow that cannot validate or version is a generator
regression even when every unit test is green.

Asserts contract validity of the generator's output, NOT quality: the
generated workflow must load with zero validation issues and must version
through the save-time cliff. Stage count, shape, and prompt wording are
deliberately unasserted. Running the generated workflow is out of scope here
(that is the sandbox-smoke-run design's territory).

When the backend is missing this FAILS, never skips — same policy as tier 2.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.loader import load_workflow_object
from app.services.project import create_project
from test_journey_smoke import _point_examples_dir_at

pytestmark = pytest.mark.live_generation

PROJECT = "gen_smoke"
GENERATION_TIMEOUT_S = 600
POLL_S = 10

# Small on purpose (budget + a workflow reviewable on failure), but a real
# multi-stage ask: an input, an LLM judgment, a publish.
METHODOLOGY = """\
We collect public statements and check which ones concern money.

Load a CSV of statements with columns statement_id (a unique string id) and
text (the statement). For each statement, use an LLM to judge whether the
statement concerns money, payments, or revenue — record the judgment as a
boolean that may be null when the text is too ambiguous to call. Publish the
judged table as a CSV report.
"""


def test_generated_workflow_validates_and_versions(tmp_path, monkeypatch):
    # The compiler agents need the claude CLI + claude-agent-sdk (same condition
    # as app.runtime.options.agent_available, computed directly here because
    # conftest monkeypatches that function to False for the offline suite).
    import importlib.util
    import shutil
    from app.core.llm_sdk import CLI_PATH
    assert (shutil.which("claude") or CLI_PATH) and importlib.util.find_spec(
        "claude_agent_sdk"
    ), (
        "live_generation tier needs the compiler agent backend (claude-agent-sdk "
        "+ claude CLI). This tier fails rather than skips."
    )

    _point_examples_dir_at(monkeypatch, tmp_path)
    create_project(PROJECT, METHODOLOGY, source="generation smoke test")
    project_dir = tmp_path / PROJECT
    compiled_dir = project_dir / "compiled"

    # Lifespan-scoped client: the generation turn runs on the app's event loop,
    # which stays alive for the whole `with` block while we poll the filesystem
    # from the test thread. Stages land on disk only when the turn completes.
    with TestClient(app) as client:
        resp = client.post(f"/project/{PROJECT}/generate-workflow", follow_redirects=False)
        assert resp.status_code in (302, 303), resp.text

        deadline = time.monotonic() + GENERATION_TIMEOUT_S
        while time.monotonic() < deadline:
            if compiled_dir.is_dir() and list(compiled_dir.glob("*.json")):
                break
            time.sleep(POLL_S)
        else:
            pytest.fail(
                f"generation produced no compiled/ stages within "
                f"{GENERATION_TIMEOUT_S}s — inspect the chat session transcript "
                f"under the project's sessions for the agent's failure"
            )

        # The gate: the CURRENT model accepts everything the CURRENT generator
        # emits. Any palm-class drift (relative path, dead connector kind,
        # double-braced prompt) raises WorkflowLoadError with the issue list.
        load_workflow_object(project_dir)

        # And the save-time cliff (stage-test gate + config-column checks):
        # a generated workflow the reviewer cannot even version is a regression.
        resp = client.post(f"/project/{PROJECT}/version", data={"message": "generation smoke"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True, resp.text
