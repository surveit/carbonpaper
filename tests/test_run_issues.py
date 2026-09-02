"""The run page's issue index (app/web/run_issues.py + _run_issues.html).
ONE list: the stop that ended the run is a line of it like any other. The two
three failures a stop can be reporting — the stage's output, the input its author
refused, the code — must still read apart at a glance, because they route to
different people.
"""
from __future__ import annotations

from typing import Any

from app.models import StepRefused, parse_stage
from app.models.run_manifest import SCHEMA_REFUSAL_ERROR_TYPE
from app.web.config import templates
from app.web.panel_links import AppPanelLinks
from app.web.run_issues import StopKind, build_run_issues

PROJECT = "issues"
RUN = "20260806T090100"
ENUM_MESSAGE = (
    "12 value(s) outside enum ['A', 'B', 'C'] (e.g. 'venezuela')"
)


def _stage(stage_id: str, inputs: list[str]) -> dict[str, Any]:
    if not inputs:
        return {
            "id": stage_id, "description": stage_id, "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": "/tmp/rows.csv", "format": "csv"}},
            "signature": {
                "form": "replaces",
                "produces": [{"name": "issue_type", "type": "str", "nullable": False}],
            },
        }
    return {
        "id": stage_id, "description": stage_id, "type": "python_row_function",
        "inputs": [
            {"id": producer}
            for producer in inputs
        ],
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return row\n"},
        "signature": {"form": "extends", "adds": []},
    }


def _stages(*specs: tuple[str, list[str]]):
    return [parse_stage(_stage(stage_id, inputs)) for stage_id, inputs in specs]


def _report(phase: str, *issues: tuple[str, str | None, str]) -> dict[str, Any]:
    return {
        "stage_id": "x", "phase": phase, "rows": 12, "ok": True,
        "issues": [{"severity": s, "column": c, "message": m} for s, c, m in issues],
    }


def _record(stage_id: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "stage_id": stage_id, "type": "python_row_function", "status": status,
        "input_validation_report": [], "output_validation_report": None,
        "output_row_count": 0, **extra,
    }


def _manifest(*records: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": RUN, "started_at": "2026-08-06T09:01:00", "project": PROJECT,
        "workflow_version": "20260806T085500", "status": "errors",
        "human_review_queue_stats": {}, "stage_records": list(records),
    }


def _refusal(stage_id: str) -> dict[str, Any]:
    return _record(
        stage_id, "error",
        error={"type": SCHEMA_REFUSAL_ERROR_TYPE,
               "message": f"stage '{stage_id}' output violates its declared output_schema",
               "traceback": None},
        output_validation_report=_report("output", ("error", "issue_type", ENUM_MESSAGE)),
    )


def _crash(stage_id: str) -> dict[str, Any]:
    return _record(
        stage_id, "error",
        error={"type": "KeyError", "message": "'client_name'",
               "traceback": "Traceback (most recent call last):\n  ..."},
    )


REFUSAL_REASON = "the counting steps produced ['manual_merge_rules'], which this workbook has no wording for"


def _refused(stage_id: str) -> dict[str, Any]:
    return _record(
        stage_id, "error",
        error={"type": StepRefused.__name__, "message": REFUSAL_REASON,
               "traceback": "Traceback (most recent call last):\n  ..."},
    )


# ─── Section 1: what stopped the run ────────────────────────────────────────

def test_a_data_refusal_reads_as_the_data_question_the_message_already_asks():
    issues = build_run_issues(_manifest(_refusal("classify_issues")), None)

    stop = issues.stopped[0]
    assert stop.stage_id == "classify_issues"
    assert stop.kind is StopKind.schema
    # The line is the report's own wording — the panel and the index cannot drift.
    assert [(i.column, i.message) for i in stop.issues] == [("issue_type", ENUM_MESSAGE)]


def test_a_transform_exception_stays_engineer_facing_in_the_same_section():
    issues = build_run_issues(_manifest(_crash("publish_report")), None)

    stop = issues.stopped[0]
    assert stop.kind is StopKind.crash
    assert (stop.error_type, stop.error_message) == ("KeyError", "'client_name'")


def test_an_authored_refusal_is_the_datas_story_not_the_codes():
    stop = build_run_issues(_manifest(_refused("publish_workbook")), None).stopped[0]

    assert stop.kind is StopKind.refused
    assert stop.error_message == REFUSAL_REASON


def test_a_stop_names_the_downstream_stages_that_never_ran():
    manifest = _manifest(
        _record("load", "ok"),
        _refusal("classify_issues"),
        _record("rank_by_spend", "pending"),
        _record("publish_report", "pending"),
    )
    stages = _stages(
        ("load", []), ("classify_issues", ["load"]),
        ("rank_by_spend", ["classify_issues"]), ("publish_report", ["rank_by_spend"]),
    )

    assert build_run_issues(manifest, stages).stopped[0].never_ran == [
        "rank_by_spend", "publish_report"
    ]


def test_a_pending_stage_on_another_fork_is_not_blamed_on_this_stop():
    manifest = _manifest(
        _record("load", "ok"),
        _refusal("classify_issues"),
        _record("await_review", "awaiting_review"),
        _record("publish_review", "pending"),
    )
    stages = _stages(
        ("load", []), ("classify_issues", ["load"]),
        ("await_review", ["load"]), ("publish_review", ["await_review"]),
    )

    assert build_run_issues(manifest, stages).stopped[0].never_ran == []


def test_an_unreadable_version_drops_the_never_ran_clause_rather_than_guessing():
    manifest = _manifest(_refusal("classify_issues"), _record("report", "pending"))

    assert build_run_issues(manifest, None).stopped[0].never_ran == []


# ─── Section 2: what else is worth a look ───────────────────────────────────

def test_warnings_are_one_line_per_stage_column_message_not_one_per_row():
    manifest = _manifest(_record(
        "score", "validation_warnings",
        output_validation_report=_report(
            "output", ("warning", "spend", "10000 value(s) outside range [0, 1000000]")),
    ))

    flagged = build_run_issues(manifest, None).flagged
    assert [(f.stage_id, len(f.issues)) for f in flagged] == [("score", 1)]
    assert "10000 value(s)" in flagged[0].issues[0].message


def test_the_same_line_raised_on_both_sides_of_a_stage_is_one_entry():
    passthrough = ("warning", None, "1 undeclared column(s) present: ['note']")
    manifest = _manifest(_record(
        "flag", "ok",
        input_validation_report=[_report("input:load", passthrough)],
        output_validation_report=_report("output", passthrough),
    ))

    issue = build_run_issues(manifest, None).flagged[0].issues[0]
    assert issue.phases == ["input:load", "output"]


def test_a_stopped_stages_warnings_still_show_up_under_worth_a_look():
    record = _refusal("classify_issues")
    record["input_validation_report"] = [
        _report("input:load", ("warning", "note", "3 value(s) outside range [0, 9]"))
    ]
    issues = build_run_issues(_manifest(record), None)

    assert [i.severity for i in issues.stopped[0].issues] == ["error"]
    assert [i.message for i in issues.flagged[0].issues] == [
        "3 value(s) outside range [0, 9]"
    ]


def test_an_error_that_did_not_stop_the_run_is_still_indexed():
    manifest = _manifest(_record(
        "score", "validation_warnings",
        input_validation_report=[
            _report("input:load", ("error", "spend", "4 value(s) not of declared type"))
        ],
    ))

    assert build_run_issues(manifest, None).flagged[0].issues[0].severity == "error"


def test_a_clean_run_has_no_index_at_all():
    issues = build_run_issues(_manifest(_record("load", "ok")), None)

    assert (issues.stopped, issues.flagged) == ([], [])


# ─── What the section renders ───────────────────────────────────────────────

def _render(manifest: dict[str, Any], stages: Any = None) -> str:
    return templates.env.get_template("_run_issues.html").render(
        project_id=PROJECT, run_id=RUN,
        issues=build_run_issues(manifest, stages),
        links=AppPanelLinks(PROJECT, RUN),
    )


def test_each_stop_story_is_worded_apart_in_the_markup():
    assert "the data changed" in _render(_manifest(_refusal("classify_issues")))
    assert ("this stage does not handle this data"
            in _render(_manifest(_refused("publish_workbook"))))
    assert "the code broke" in _render(_manifest(_crash("publish_report")))


def test_a_data_refusal_deep_links_the_panels_data_tab():
    html = _render(_manifest(_refusal("classify_issues")))

    assert 'data-stage-link="classify_issues"' in html
    assert 'data-stage-tab="data"' in html


def test_a_crash_deep_links_the_panels_transform_tab_instead():
    assert 'data-stage-tab="transform"' in _render(_manifest(_crash("publish_report")))


def test_an_authored_refusal_leads_with_its_reason_and_not_its_exception_name():
    html = _render(_manifest(_refused("publish_workbook")))

    assert "this stage does not handle this data" in html
    assert "which this workbook has no wording for" in html
    assert StepRefused.__name__ not in html
    assert 'data-stage-tab="transform"' in html


def test_the_flagged_section_is_titled_by_its_counts_by_severity():
    manifest = _manifest(
        _record("score", "validation_warnings", output_validation_report=_report(
            "output",
            ("warning", "spend", "off range"),
            ("warning", "note", "3 undeclared column(s) present"),
        )),
        _record("flag", "validation_warnings", input_validation_report=[_report(
            "input:score", ("error", "spend", "4 value(s) not of declared type"))]),
    )

    assert "2 warnings, 1 error" in _render(manifest)


def test_a_severity_nothing_raised_is_left_out_of_the_title_not_counted_as_zero():
    manifest = _manifest(_record(
        "score", "validation_warnings",
        output_validation_report=_report("output", ("warning", "spend", "off range")),
    ))
    html = _render(manifest)

    assert "1 warning<" in html
    assert "error" not in html


def test_a_stop_is_a_line_of_the_same_list_as_the_warnings_it_did_not_cause():
    html = _render(_manifest(
        _refusal("classify_issues"),
        _record("score", "validation_warnings",
                output_validation_report=_report(
                    "output", ("warning", "spend", "off range"))),
    ))

    assert html.count('<table class="issue-table"') == 1
    assert "1 warning, 1 error" in html
    assert "<code>stopped</code>" in html


def test_what_only_a_stop_carries_is_nested_under_its_own_line():
    html = _render(_manifest(_crash("publish_report")))

    assert html.count("<tr ") == 1
    assert '<div class="issue-more">' in html
    assert "Traceback (most recent call last)" not in html


def test_the_index_opens_closed_with_its_counts_still_on_screen():
    html = _render(_manifest(
        _refusal("classify_issues"),
        _record("score", "validation_warnings",
                output_validation_report=_report(
                    "output", ("warning", "spend", "off range"))),
    ))

    assert "<details class=\"issue-panel" in html
    assert " open>" not in html
    # The fold hides the table, never the fact that there is one to open.
    assert "1 warning, 1 error" in html
    assert "off range" in html    # present in the markup, behind the disclosure
