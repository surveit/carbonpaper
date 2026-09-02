"""The stage panel's Error line: only a crash shows its exception type."""
from __future__ import annotations

from app.models import StepRefused
from app.models.run_manifest import SCHEMA_REFUSAL_ERROR_TYPE
from app.web.config import templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH
from app.web.panel_links import AppPanelLinks

REASON = "the counting steps produced a merge rule which this workbook has no wording for"


def _render(error_type: str, message: str) -> str:
    return templates.env.get_template("_run_stage_panel.html").render(
        stage={
            "stage_id": "publish_workbook", "type": "python_row_function",
            "status": "error", "elapsed_ms": 5, "output_row_count": 0,
            "error": {"type": error_type, "message": message},
        },
        type_glyph=TYPE_GLYPH, type_class=TYPE_CLASS,
        links=AppPanelLinks("panel", "20260806T090100"),
        project="panel", run_id="20260806T090100",
    )


def test_an_authored_refusal_shows_its_reason_without_its_exception_name():
    html = _render(StepRefused.__name__, REASON)

    assert REASON in html
    assert StepRefused.__name__ not in html


def test_a_schema_stop_shows_its_reason_without_its_exception_name():
    html = _render(SCHEMA_REFUSAL_ERROR_TYPE, "issue_type: 12 value(s) outside enum")

    assert "12 value(s) outside enum" in html
    assert SCHEMA_REFUSAL_ERROR_TYPE not in html


def test_a_crash_keeps_the_exception_name_because_nothing_else_names_it():
    assert "<strong>KeyError</strong>" in _render("KeyError", "reported_amount missing")
