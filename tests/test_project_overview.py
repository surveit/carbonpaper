"""The latest-run card: the figures it produced, and what a reader must know about them."""
from __future__ import annotations

from app.core.run_status import RunStatus
from app.models.claims import StageOutputCellCitation
from app.models.records.run_manifest import RunManifest
from app.models.records.workflow_output import WorkflowOutput
from app.models.run_parameters import RunParameters
from app.web.project_overview import build_deliverable, build_queue, read_versions
from app.web.run_index import build_run_index_rows

_PROJECT = "venezuela_lobbying_q1_q2_2026"
_RUN = "20260825T132644.199715"


def _record_run(*, status: RunStatus = RunStatus.OK,
                parameters: RunParameters | None = None) -> None:
    RunManifest(
        id=RunManifest.compose_id(_PROJECT, _RUN),
        run_id=_RUN, started_at="2026-08-25T13:26:44", project=_PROJECT,
        workflow_version="20260825T132440.925374",
        parameters=parameters or RunParameters(),
        input_bindings={}, human_review_queue_stats={}, dropped_columns={},
        status=status, stage_records=[],
    ).save()


def _publish_a_figure() -> None:
    WorkflowOutput(
        slug="paid-filings", label="Paid filings behind that total", primary=True,
        citation=StageOutputCellCitation(
            run_id=_RUN, stage_id="paid_totals", row_ordinal=0,
            column="filings", value=40,
        ),
    ).save()


def _rows():
    return [row for row in build_run_index_rows(_PROJECT) if not row.is_test_run]


def _build():
    return build_deliverable(_PROJECT, _rows(), read_versions(_PROJECT))


def test_a_project_with_no_run_is_told_so_and_offered_the_run():
    deliverable = _build()
    assert deliverable.state == "no_runs"
    assert deliverable.lead


def test_a_clean_run_leads_with_its_figures_and_nothing_to_warn_about():
    _record_run()
    _publish_a_figure()
    deliverable = _build()
    assert deliverable.state == "clean"
    assert [f.value for f in deliverable.published.figures] == ["40"]
    assert [check.ok for check in deliverable.checks] == [True]


def test_a_capped_run_warns_and_names_the_stage_it_windowed():
    _record_run(parameters=RunParameters(limits={"ingest_normalize": 40}))
    _publish_a_figure()
    deliverable = _build()
    assert deliverable.state == "warned"
    capped = [check for check in deliverable.checks if check.headline == "This run was capped."]
    assert "ingest_normalize" in capped[0].detail
    assert capped[0].action.kind == "chat"


def test_a_running_run_is_not_said_to_have_ended():
    _record_run(status=RunStatus.RUNNING)
    _publish_a_figure()
    assert [c.headline for c in _build().checks] == ["This run has not completed."]


def test_a_halted_run_is_offered_its_review_rather_than_a_chat():
    _record_run(status=RunStatus.AWAITING_REVIEW)
    _publish_a_figure()
    waiting = _build().checks[0]
    assert waiting.headline == "This run is waiting on a review."
    assert waiting.action.kind == "go" and waiting.action.href.endswith(_RUN)


def test_a_run_that_wrote_no_figure_says_why_none_can_be_added_now():
    _record_run()
    warning = _build().checks[0]
    assert warning.headline == "This run produced no figures."
    assert warning.action.kind == "chat"


def test_a_queue_row_that_is_not_an_app_screen_opens_a_chat_carrying_the_task():
    _record_run(status=RunStatus.ERRORS)
    errored = [row for row in build_queue(_PROJECT, _rows(), read_versions(_PROJECT))
               if row.what.endswith("errored")]
    assert errored[0].kind == "chat"
    assert "task=" in errored[0].href


def test_the_run_rows_link_to_the_runs_they_are_about():
    _record_run(status=RunStatus.AWAITING_REVIEW)
    queue = build_queue(_PROJECT, _rows(), read_versions(_PROJECT))
    review = [row for row in queue if row.label == "Review"]
    assert review[0].href.endswith("/runs?status=awaiting_review")
