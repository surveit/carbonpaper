"""The claims step against a fake workspace instead of a server."""
from __future__ import annotations

import json
from typing import Any

import pytest

from evals.rebuild_check import claims_stage
from evals.rebuild_check.eval_pieces import render_claims_code

_RUN_URL = "http://127.0.0.1:9999/project/inner/runs/built"
_ANSWER = {"total_cases": 17, "share": 31.8, "peak_year": 2011}
_QUOTES = {"total_cases": "Seventeen cases", "share": "more than 30 percent",
           "peak_year": "their peak in 2011"}


class _Workspace:
    """Plays submit_claim and read_claim_review, and records every call."""

    def __init__(self, *, review_polls: int = 2, refused: frozenset[str] = frozenset()):
        self.calls: list[tuple[str, dict]] = []
        self.review_polls = review_polls
        self.refused = refused
        self.polls: dict[str, int] = {}
        self.most_in_flight = 0

    def __call__(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if name == "submit_claim":
            if arguments["slug"] in self.refused:
                raise claims_stage.ToolError("run 'built' published no claimable output")
            claim_id = "claim-" + arguments["slug"]
            self.polls[claim_id] = 0
            running = sum(1 for n in self.polls.values() if n < self.review_polls)
            self.most_in_flight = max(self.most_in_flight, running)
            return {"claim": {"id": claim_id}, "claim_url": "/project/inner/claims/" + claim_id}
        self.polls[arguments["claim_id"]] += 1
        done = self.polls[arguments["claim_id"]] >= self.review_polls
        return {"review": "done" if done else "running", "error": None,
                "challenges": [{"severity": 2}] if done else []}


def _row(*, diagnosis=(), quotes=None, citation=True) -> dict:
    return {"item_id": "abc", "run_url": _RUN_URL, "citation_holds": citation,
            "results_json": json.dumps(_ANSWER),
            "expected_quotes_json": json.dumps(_QUOTES if quotes is None else quotes),
            "diagnosis": json.dumps(list(diagnosis))}


@pytest.fixture
def workspace(monkeypatch):
    def install(**kwargs: Any) -> _Workspace:
        fake = _Workspace(**kwargs)
        monkeypatch.setattr(claims_stage, "call_tool", fake)
        monkeypatch.setattr(claims_stage, "POLL_SECONDS", 0)
        return fake
    return install


def test_each_figure_becomes_a_reviewed_claim_on_the_graded_run(workspace):
    fake = workspace()
    claims = json.loads(claims_stage.transform(_row())["claims_json"])
    assert {c["field"] for c in claims} == set(_ANSWER)
    assert all(c["review"] == "done" for c in claims)
    assert all(a["run_id"] == "built" for n, a in fake.calls if n == "submit_claim")


def test_our_defects_and_unquoted_figures_are_skipped_with_a_reason(workspace):
    workspace()
    out = claims_stage.transform(_row(
        diagnosis=[{"field": "share", "verdict": "our_defect"}],
        quotes={"total_cases": "Seventeen cases"}))
    assert json.loads(out["claims_skipped_json"]) == {
        "share": "ruled our_defect", "peak_year": "no quote recorded"}


def test_no_more_than_two_reviews_run_at_once(workspace):
    fake = workspace(review_polls=3)
    claims_stage.transform(_row())
    assert fake.most_in_flight <= 2


def test_a_refused_submission_is_recorded_and_not_retried(workspace):
    fake = workspace(refused=frozenset({"share"}))
    claims = {c["field"]: c for c in json.loads(claims_stage.transform(_row())["claims_json"])}
    assert "no claimable output" in claims["share"]["error"]
    assert [a["slug"] for n, a in fake.calls if n == "submit_claim"].count("share") == 1


def test_a_case_whose_citation_failed_makes_no_calls(workspace):
    fake = workspace()
    claims_stage.transform(_row(citation=False))
    assert fake.calls == []


def test_the_rendered_stage_code_carries_its_server():
    namespace: dict[str, Any] = {}
    exec(render_claims_code("http://127.0.0.1:9999/mcp"), namespace)
    assert namespace["MCP_URL"] == "http://127.0.0.1:9999/mcp"


def test_the_module_refuses_to_run_without_a_server(monkeypatch):
    monkeypatch.setattr(claims_stage, "MCP_URL", None)
    with pytest.raises(ValueError, match="MCP_URL"):
        claims_stage.call_tool("read_claim_review", {"project_id": "inner", "claim_id": "c"})
