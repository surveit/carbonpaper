"""Publishing a run: the three things that refuse one, and the record that says it happened."""
from __future__ import annotations

import pytest

from app.core.run_status import RunStatus
from app.models.claims import StageOutputCellCitation
from app.models.records.published_run import PublishedRun
from app.models.records.run_manifest import RunManifest
from app.models.records.workflow_output import WorkflowOutput
from app.models.run_parameters import RunParameters
from app.services.run_publication import (
    RunNotPublishable,
    find_publish_refusals,
    publish_run,
    read_published_run,
    withdraw_run,
)

_PROJECT = "venezuela_lobbying_q1_q2_2026"
_RUN = "20260825T132644.199715"
_STAGE = "paid_totals"


def _record_run(run_id: str = _RUN, *, status: RunStatus = RunStatus.OK,
                parameters: RunParameters | None = None) -> None:
    RunManifest(
        id=RunManifest.compose_id(_PROJECT, run_id),
        run_id=run_id, started_at="2026-08-25T13:26:44", project=_PROJECT,
        workflow_version="20260825T132440.925374",
        parameters=parameters or RunParameters(),
        input_bindings={}, human_review_queue_stats={}, dropped_columns={},
        status=status, stage_records=[],
    ).save()


def _publish_a_figure(run_id: str = _RUN) -> None:
    WorkflowOutput(
        slug="paid-filings", label="Paid filings behind that total", primary=True,
        citation=StageOutputCellCitation(
            run_id=run_id, stage_id=_STAGE, row_ordinal=0, column="filings", value=40,
        ),
    ).save()


def test_a_complete_run_that_wrote_a_figure_is_publishable():
    _record_run()
    _publish_a_figure()
    assert find_publish_refusals(_PROJECT, _RUN) == []


def test_publishing_records_the_run_and_repeats_harmlessly():
    _record_run()
    _publish_a_figure()
    first = publish_run(_PROJECT, _RUN)
    assert publish_run(_PROJECT, _RUN).id == first.id
    assert read_published_run(_PROJECT, _RUN) is not None
    assert PublishedRun.find(project_id=_PROJECT) != []


def test_withdrawing_leaves_the_figures_where_the_run_wrote_them():
    _record_run()
    _publish_a_figure()
    publish_run(_PROJECT, _RUN)
    withdraw_run(_PROJECT, _RUN)
    assert read_published_run(_PROJECT, _RUN) is None
    assert [o.slug for o in WorkflowOutput.list()] == ["paid-filings"]


def test_a_capped_run_is_refused_however_cleanly_it_finished():
    _record_run(parameters=RunParameters(limits={"ingest_normalize": 40}))
    _publish_a_figure()
    refusals = find_publish_refusals(_PROJECT, _RUN)
    assert [r.kind for r in refusals] == ["windowed"]
    assert "ingest_normalize" in refusals[0].detail


def test_a_test_run_is_refused_the_same_way_a_capped_one_is():
    _record_run(parameters=RunParameters(is_test_run=True))
    _publish_a_figure()
    assert [r.kind for r in find_publish_refusals(_PROJECT, _RUN)] == ["windowed"]


def test_a_run_that_did_not_finish_clean_is_refused():
    _record_run(status=RunStatus.AWAITING_REVIEW)
    _publish_a_figure()
    assert [r.kind for r in find_publish_refusals(_PROJECT, _RUN)] == ["incomplete"]


def test_a_run_that_wrote_no_figure_is_refused():
    _record_run()
    assert [r.kind for r in find_publish_refusals(_PROJECT, _RUN)] == ["no_figures"]


def test_publishing_a_refused_run_raises_and_records_nothing():
    _record_run(parameters=RunParameters(limits={"ingest_normalize": 40}))
    _publish_a_figure()
    with pytest.raises(RunNotPublishable):
        publish_run(_PROJECT, _RUN)
    assert read_published_run(_PROJECT, _RUN) is None


def test_a_running_run_is_not_said_to_have_ended():
    _record_run(status=RunStatus.RUNNING)
    _publish_a_figure()
    refusals = find_publish_refusals(_PROJECT, _RUN)
    assert [r.headline for r in refusals] == ["This run has not completed."]


def test_a_run_that_really_ended_says_how():
    _record_run(status=RunStatus.ERRORS)
    _publish_a_figure()
    assert find_publish_refusals(_PROJECT, _RUN)[0].headline == "This run ended errors."
