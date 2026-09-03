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
def test_the_catalog_no_longer_offers_the_python_types():
    for name in APPROVAL_REQUIRED_TYPES:
        assert name not in AUTHORABLE_TYPES
        assert f"- {name} —" not in render_type_catalog()


def test_the_catalog_still_says_they_exist_and_how_to_ask():
    """The door needs a doorbell: withheld silently, a stuck model concludes it is impossible."""
    catalog = render_type_catalog()
    for name in APPROVAL_REQUIRED_TYPES:
        assert name in catalog, name
    assert "approve_code_execution" in catalog
    assert "WAIT for their answer" in catalog


def test_the_catalog_says_the_row_type_is_the_safer_of_the_two():
    assert "the row one keeps the trace" in render_type_catalog()


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


def test_a_python_row_function_is_gated_too():
    row_stage = dict(_FRAME_STAGE, type="python_row_function")
    assert stage_edit.find_unapproved_code_issues("some-project", row_stage) != []


def test_only_the_frame_function_is_charged_with_ending_the_trace():
    """The row function is grain-and-order preserving and the filter records its cuts."""
    for kept in ("python_row_function", "filter_rows"):
        refusal = stage_edit.find_unapproved_code_issues(
            "some-project", dict(_FRAME_STAGE, type=kept))[0]
        assert "trace" not in refusal, kept
        assert "network" in refusal, kept


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


# ── maintaining a stage that already exists ───────────────────────────────────
def test_a_stage_already_of_this_type_stays_editable_without_approval():
    """Refusing this would strand every project holding one — 492 stages do."""
    stored = {"reshape": dict(_FRAME_STAGE, description="the old description")}
    edited = dict(_FRAME_STAGE, description="a corrected description")
    assert stage_edit.find_unapproved_code_issues("p1", edited, stored) == []


def test_changing_a_stage_INTO_a_gated_type_is_still_refused():
    stored = {"reshape": dict(_FRAME_STAGE, type="starlark_row_function")}
    assert stage_edit.find_unapproved_code_issues("p1", _FRAME_STAGE, stored) != []


def test_a_new_stage_is_refused_even_when_others_of_the_type_are_stored():
    stored = {"elsewhere": dict(_FRAME_STAGE, id="elsewhere")}
    assert stage_edit.find_unapproved_code_issues("p1", _FRAME_STAGE, stored) != []


# ── filter_rows joins the gate ────────────────────────────────────────────────
_FILTER_STAGE = {
    "id": "in_scope", "description": "in_scope", "type": "filter_rows",
    "inputs": [{"id": "filings"}],
    "signature": {"form": "extends", "reads": [{"input": "filings", "columns": [
        {"name": "status", "type": "str", "nullable": False}]}]},
    "filter": {"summary": "Keeps filings still active.",
               "code": 'def should_include(row):\n    return row["status"] == "Active"\n'},
}


def test_the_python_filter_is_gated_like_the_other_python_types():
    assert "filter_rows" in APPROVAL_REQUIRED_TYPES
    assert stage_edit.find_unapproved_code_issues("p1", _FILTER_STAGE) != []


def test_the_sandboxed_filter_is_the_offered_one():
    assert "starlark_filter_rows" in AUTHORABLE_TYPES
    assert "filter_rows" not in AUTHORABLE_TYPES


def test_report_is_now_the_only_unsandboxed_type_still_offered():
    """The burn-down's last step, pinned so finishing it shows up here."""
    from app.models.stages.stage_types import AUTHORABLE_CODE_CARRYING_TYPES
    unsandboxed = [t for t in AUTHORABLE_CODE_CARRYING_TYPES if not t.startswith("starlark_")]
    assert unsandboxed == ["report"]
