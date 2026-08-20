"""The gate on python_frame_function: withheld from every authoring catalog, refused
on write until the project's owner has approved it, and still loading and running for
the workflows that already carry one."""
from __future__ import annotations

import pytest

from app.models import parse_stage
from app.models.stages.stage_types import (
    APPROVAL_REQUIRED_TYPES,
    AUTHORABLE_TYPES,
    STAGE_TYPES,
)
from app.services import code_approval, stage_edit
from app.tools.prompt_fragments import render_type_catalog

_FRAME_STAGE = {
    "id": "reshape", "description": "reshape", "type": "python_frame_function",
    "inputs": [{"id": "filings"}],
    "signature": {"form": "replaces",
                  "produces": [{"name": "client", "type": "str", "nullable": False}]},
    "function": {"kind": "inline", "summary": "Collapses the register to one row per client.",
                 "code": "def transform(df):\n    return df\n"},
}


# ── withheld from the catalog, but never from the runtime ─────────────────────
def test_the_catalog_no_longer_offers_the_frame_function():
    assert "python_frame_function" not in AUTHORABLE_TYPES
    assert "python_frame_function" not in render_type_catalog()


def test_the_type_still_exists_so_stored_workflows_keep_loading():
    # Withholding it from authoring must not unmake the type: 492 stored stages carry one.
    assert "python_frame_function" in STAGE_TYPES
    assert parse_stage(_FRAME_STAGE).type == "python_frame_function"


def test_every_approval_required_type_is_a_real_type_and_is_withheld():
    for name in APPROVAL_REQUIRED_TYPES:
        assert name in STAGE_TYPES
        assert name not in AUTHORABLE_TYPES


# ── the write-time gate ───────────────────────────────────────────────────────
def test_a_frame_function_is_refused_without_approval():
    issues = stage_edit.find_unapproved_code_issues("some-project", _FRAME_STAGE)
    assert len(issues) == 1
    assert "has not approved" in issues[0]


def test_the_refusal_says_what_to_try_instead_and_how_to_turn_it_on():
    refusal = stage_edit.find_unapproved_code_issues("some-project", _FRAME_STAGE)[0]
    for expected in ("explode", "starlark_row_function", "dedupe", "sort_rank",
                     "approve_code_execution"):
        assert expected in refusal, expected
    # The reader is told the two costs, not just that it is blocked.
    assert "network" in refusal and "trace stops at it" in refusal


def test_a_sandboxed_stage_is_never_gated():
    sandboxed = dict(_FRAME_STAGE, type="starlark_row_function")
    assert stage_edit.find_unapproved_code_issues("some-project", sandboxed) == []


def test_approval_opens_the_gate(fresh_store):
    code_approval.approve_code_execution("p1", "the snapshot diff has no declared form")
    assert stage_edit.find_unapproved_code_issues("p1", _FRAME_STAGE) == []


def test_withdrawing_closes_it_again(fresh_store):
    code_approval.approve_code_execution("p1", "a reason")
    code_approval.withdraw_code_execution_approval("p1")
    assert stage_edit.find_unapproved_code_issues("p1", _FRAME_STAGE) != []


def test_approval_is_per_project(fresh_store):
    code_approval.approve_code_execution("p1", "a reason")
    assert code_approval.has_code_execution_approval("p2") is False


# ── what the approval record has to carry ─────────────────────────────────────
def test_approving_without_a_reason_is_refused(fresh_store):
    with pytest.raises(ValueError, match="needs the reason"):
        code_approval.approve_code_execution("p1", "   ")


def test_the_reason_is_kept_for_whoever_revokes_later(fresh_store):
    code_approval.approve_code_execution("p1", "diffing two roster snapshots")
    assert code_approval.read_code_execution_approval("p1").reason == (
        "diffing two roster snapshots")


def test_approving_twice_keeps_the_first_answer(fresh_store):
    first = code_approval.approve_code_execution("p1", "the original reason")
    again = code_approval.approve_code_execution("p1", "a different reason")
    assert again.approved_at == first.approved_at
    assert again.reason == "the original reason"
