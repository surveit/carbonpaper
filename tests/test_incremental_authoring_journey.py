"""End-to-end walkthrough of the INCREMENTAL authoring loop (#243), driven through
the real MCP tool functions against a seeded example project.

This stands in for the issue's "drive a seeded example project end-to-end from a
Claude Code session" dogfood item: it exercises the exact tool functions an MCP
client calls — `app.mcp.server.describe_workflow / read_stage / add_stage /
edit_stage / remove_stage` — over the committed lobbying_issue_triage fixture and
over a from-scratch project, asserting the loop's invariants at each step:

  * a project with NO workflow yet accepts its first `add_stage` (there is no
    one-shot generator left to create one);
  * `read_stage` on the upstream is what grounds a new stage's declared input
    schema, and a declaration the upstream does not satisfy is REFUSED by name
    with nothing written (edge conformance, #214);
  * `edit_stage` changes only the fields it names;
  * `remove_stage` refuses while a dependent still inputs from the stage, and
    succeeds once the dependent is gone;
  * every write leaves the workflow strictly loadable, and a refused write leaves
    the bytes on disk untouched.

Offline: no LLM, no chat turn — only the validated stage writers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.mcp import server
from app.services import workspace
from app.services.loader import load_workflow
from app.services.project import WorkflowFile, import_project

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "seeds" / "data" / "lobbying_issue_triage.json"
)
_SEEDED_STAGE_IDS = ["raw_filings", "classify_issues", "rank_by_spend", "publish_report"]

# A new leaf stage bucketing each ranked filing by spend. Its declared input schema
# names only the two columns it consumes, both of which `rank_by_spend` really
# outputs (str filing_id, int amount_usd) — the projection an author writes AFTER
# read_stage'ing the upstream, never guessed.
_BAND_STAGE: dict[str, Any] = {
    "id": "spend_band",
    "type": "python_row_function",
    "name": "Band filings by reported spend",
    "inputs": [{
        "id": "rank_by_spend",
        "schema": {"columns": [
            {"name": "filing_id", "type": "str", "nullable": False},
            {"name": "amount_usd", "type": "int", "nullable": False},
        ]},
    }],
    "output_schema": {
        "columns": [
            {"name": "filing_id", "type": "str", "nullable": False},
            {"name": "amount_usd", "type": "int", "nullable": False},
            {"name": "band", "type": "str", "nullable": False},
        ],
        "primary_key": ["filing_id"],
    },
    "function": {
        "kind": "inline",
        "code": (
            "def transform(row):\n"
            "    amount = row['amount_usd']\n"
            "    band = 'high' if amount >= 100000 else 'low'\n"
            "    return {'filing_id': row['filing_id'], 'amount_usd': amount, 'band': band}\n"
        ),
    },
}


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """The committed lobbying fixture imported into a tmp workspace, with the
    name-based service surface (and therefore every MCP tool) pointed at it."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", examples_dir)
    wf = WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return import_project(wf, name="lobbying", examples_dir=examples_dir)


def _stage_ids(project_id: str) -> list[str]:
    return [stage["id"] for stage in server.describe_workflow(project_id=project_id)["stages"]]


def _compiled_dir(project_id: str) -> Path:
    return workspace.resolve_project_dir(project_id) / "compiled"


# ─── the loop over a seeded project ──────────────────────────────────────────


def test_add_edit_remove_round_trip_on_a_seeded_project(seeded: str) -> None:
    """describe → read upstream → add → edit → remove, the whole loop, asserting
    the workflow stays strictly loadable at every step."""
    # 1. Read what is there. This is the client's first call, every time.
    assert _stage_ids(seeded) == _SEEDED_STAGE_IDS

    # 2. Read the UPSTREAM producer before authoring against it — its output_schema
    #    is the only truthful source of the columns the new stage may declare.
    upstream = json.loads(server.read_stage(project_id=seeded, stage_id="rank_by_spend"))
    upstream_columns = {col["name"]: col for col in upstream["output_schema"]["columns"]}
    for declared in _BAND_STAGE["inputs"][0]["schema"]["columns"]:
        assert declared["name"] in upstream_columns
        assert declared["type"] == upstream_columns[declared["name"]]["type"]

    # 3. Add the stage. One stage, whole JSON, validated against the whole workflow.
    assert server.add_stage(project_id=seeded, stage_json=json.dumps(_BAND_STAGE)) == {
        "ok": True, "issues": [],
    }
    assert set(_stage_ids(seeded)) == set(_SEEDED_STAGE_IDS) | {"spend_band"}
    assert {s.id for s in load_workflow(workspace.resolve_project_dir(seeded))} == set(
        _SEEDED_STAGE_IDS
    ) | {"spend_band"}

    # A new node is born amber for a human — the agent cannot approve it.
    added = next(
        s for s in server.describe_workflow(project_id=seeded)["stages"] if s["id"] == "spend_band"
    )
    assert added["review_state"] == "unreviewed"

    # 4. Edit exactly one field; everything else is preserved verbatim.
    before = json.loads(server.read_stage(project_id=seeded, stage_id="spend_band"))
    assert server.edit_stage(
        project_id=seeded, stage_id="spend_band",
        changes_json=json.dumps({"name": "Band filings by spend (revised)"}),
    ) == {"ok": True, "issues": []}
    after = json.loads(server.read_stage(project_id=seeded, stage_id="spend_band"))
    assert after["name"] == "Band filings by spend (revised)"
    assert {k: v for k, v in after.items() if k != "name"} == {
        k: v for k, v in before.items() if k != "name"
    }

    # 5. Remove it again — the undo. The graph is clean without it.
    assert server.remove_stage(project_id=seeded, stage_id="spend_band") == {
        "ok": True, "issues": [],
    }
    assert _stage_ids(seeded) == _SEEDED_STAGE_IDS
    assert {s.id for s in load_workflow(workspace.resolve_project_dir(seeded))} == set(
        _SEEDED_STAGE_IDS
    )


def test_add_stage_refuses_a_column_the_upstream_does_not_supply(seeded: str) -> None:
    """Edge conformance: declaring an input column `rank_by_spend` never outputs is
    refused BY NAME, and nothing lands on disk."""
    before = sorted(p.name for p in _compiled_dir(seeded).glob("*.json"))
    bad = json.loads(json.dumps(_BAND_STAGE))
    bad["inputs"][0]["schema"]["columns"].append(
        {"name": "not_a_real_column", "type": "str", "nullable": False}
    )

    result = server.add_stage(project_id=seeded, stage_json=json.dumps(bad))

    assert result["ok"] is False
    assert any("not_a_real_column" in issue for issue in result["issues"])
    assert sorted(p.name for p in _compiled_dir(seeded).glob("*.json")) == before
    assert "spend_band" not in _stage_ids(seeded)


def test_remove_stage_refuses_while_a_dependent_still_inputs_from_it(seeded: str) -> None:
    """The graph guard: `rank_by_spend` feeds `publish_report`, so removing it is
    refused with the dangling edge named — and the file is still there."""
    target = _compiled_dir(seeded)
    before = {p.name: p.read_text(encoding="utf-8") for p in target.glob("*.json")}

    result = server.remove_stage(project_id=seeded, stage_id="rank_by_spend")

    assert result["ok"] is False
    assert any("publish_report" in issue and "rank_by_spend" in issue for issue in result["issues"])
    assert {p.name: p.read_text(encoding="utf-8") for p in target.glob("*.json")} == before


def test_remove_stage_succeeds_once_the_dependent_is_gone(seeded: str) -> None:
    """Removing bottom-up works: drop the leaf, then its producer."""
    assert server.remove_stage(project_id=seeded, stage_id="publish_report")["ok"] is True
    assert server.remove_stage(project_id=seeded, stage_id="rank_by_spend")["ok"] is True
    assert _stage_ids(seeded) == ["raw_filings", "classify_issues"]
    assert {s.id for s in load_workflow(workspace.resolve_project_dir(seeded))} == {
        "raw_filings", "classify_issues",
    }


def test_remove_stage_on_an_unknown_id_is_loud(seeded: str) -> None:
    with pytest.raises(FileNotFoundError, match="no stage 'nope'"):
        server.remove_stage(project_id=seeded, stage_id="nope")


# ─── the loop from an EMPTY project (no one-shot generator to seed it) ───────


def test_a_workflow_can_be_authored_from_nothing_one_stage_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of #243: with `generate_workflow` gone, `add_stage` must be
    able to write the FIRST stage of a project that has no compiled/ dir at all,
    then each next stage on top of the previous one."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", examples_dir)

    project_id = server.create_project(name="From Scratch", document="Follow the money.")[
        "project_id"
    ]
    # No workflow yet: an empty summary, and the project is not listed as authored.
    assert server.describe_workflow(project_id=project_id)["stages"] == []
    assert project_id not in server.list_projects()

    source = {
        "id": "filings",
        "type": "input_data",
        "name": "Filings",
        "connector": {"kind": "file"},
        "output_schema": {
            "columns": [
                {"name": "filing_id", "type": "str", "nullable": False},
                {"name": "amount_usd", "type": "int", "nullable": False},
            ],
            "primary_key": ["filing_id"],
        },
    }
    assert server.add_stage(project_id=project_id, stage_json=json.dumps(source)) == {
        "ok": True, "issues": [],
    }
    assert _stage_ids(project_id) == ["filings"]
    assert project_id in server.list_projects()  # authored now that a stage exists

    # A second stage, wired to the first, grounded on what read_stage reports.
    produced = json.loads(server.read_stage(project_id=project_id, stage_id="filings"))
    assert [c["name"] for c in produced["output_schema"]["columns"]] == [
        "filing_id", "amount_usd",
    ]
    downstream = json.loads(json.dumps(_BAND_STAGE))
    downstream["inputs"][0]["id"] = "filings"
    assert server.add_stage(project_id=project_id, stage_json=json.dumps(downstream)) == {
        "ok": True, "issues": [],
    }
    assert sorted(_stage_ids(project_id)) == ["filings", "spend_band"]

    # And back down to empty — removing the last stage is allowed; the project is
    # simply an empty draft again, ready to be authored into.
    assert server.remove_stage(project_id=project_id, stage_id="spend_band")["ok"] is True
    assert server.remove_stage(project_id=project_id, stage_id="filings")["ok"] is True
    assert server.describe_workflow(project_id=project_id)["stages"] == []


def test_a_dangling_input_is_refused_so_stages_go_in_dependency_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a stage before its upstream exists is refused — which is what forces
    the client to author in dependency order."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", examples_dir)
    project_id = server.create_project(name="Order", document="doc")["project_id"]

    result = server.add_stage(project_id=project_id, stage_json=json.dumps(_BAND_STAGE))

    assert result["ok"] is False
    assert any("rank_by_spend" in issue for issue in result["issues"])
    assert server.describe_workflow(project_id=project_id)["stages"] == []
