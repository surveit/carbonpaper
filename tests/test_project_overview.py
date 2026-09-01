"""The deliverable slot: one place on the page, four things it says."""
from __future__ import annotations

from app.core.run_status import RunStatus
from app.models.claims import StageOutputCellCitation
from app.models.records.run_manifest import RunManifest
from app.models.records.workflow_output import WorkflowOutput
from app.models.run_parameters import RunParameters
from app.services.run_publication import publish_run
from app.web.project_overview import build_deliverable, build_queue, read_versions

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


def _build():
    from app.web.run_index import build_run_index_rows
    rows = [r for r in build_run_index_rows(_PROJECT) if not r.is_test_run]
    return build_deliverable(_PROJECT, rows, read_versions(_PROJECT))


def test_a_project_with_no_run_is_told_so_and_offered_the_run():
    deliverable = _build()
    assert deliverable.state == "no_runs"
    assert deliverable.publish_href is None
    assert deliverable.lead


def test_a_complete_run_with_a_figure_offers_publishing_and_not_the_packet():
    _record_run()
    _publish_a_figure()
    deliverable = _build()
    assert deliverable.state == "publishable"
    assert deliverable.publish_href.endswith(f"/runs/{_RUN}/publish")
    assert deliverable.packet_href is None
    assert [f.value for f in deliverable.published.figures] == ["40"]


def test_publishing_turns_on_the_packet_and_the_checks_stay_green():
    _record_run()
    _publish_a_figure()
    publish_run(_PROJECT, _RUN)
    deliverable = _build()
    assert deliverable.state == "published"
    assert deliverable.packet_href.endswith(f"/runs/{_RUN}/packet.zip")
    assert deliverable.publish_href is None
    assert all(check.ok for check in deliverable.checks)


def test_a_capped_run_is_refused_and_the_cap_is_a_failed_check():
    _record_run(parameters=RunParameters(limits={"ingest_normalize": 40}))
    _publish_a_figure()
    deliverable = _build()
    assert deliverable.state == "refused"
    assert deliverable.packet_href is None and deliverable.publish_href is None
    assert [c.ok for c in deliverable.checks] == [False]
    assert "ingest_normalize" in deliverable.checks[0].detail


def test_a_queue_row_that_is_not_an_app_screen_opens_a_chat_carrying_the_task():
    _record_run(status=RunStatus.ERRORS)
    from app.web.run_index import build_run_index_rows
    rows = [r for r in build_run_index_rows(_PROJECT) if not r.is_test_run]
    errored = [row for row in build_queue(_PROJECT, rows, read_versions(_PROJECT))
               if row.what.endswith("errored")]
    assert errored[0].kind == "chat"
    assert errored[0].href.startswith("/chat/agent/editing/new?project_id=")
    assert "task=" in errored[0].href


def test_running_runs_are_summarised_with_how_long_the_longest_has_been():
    _record_run(status=RunStatus.RUNNING)
    from app.web.run_index import build_run_index_rows
    rows = [r for r in build_run_index_rows(_PROJECT) if not r.is_test_run]
    running = [row for row in build_queue(_PROJECT, rows, read_versions(_PROJECT))
               if row.what.endswith("running")]
    assert running[0].count == "1"
    assert running[0].kind == "go"


def test_a_seeded_chat_link_opens_with_no_greeting_and_the_task_as_the_first_message():
    from app.core.agent import registry
    import app.agents.compiler.config  # noqa: F401  (registers "editing")

    context = {"project_id": _PROJECT, "base_url": "http://x/", "task": "Run it again."}
    assert registry.render_opening_turn("editing", context).text == ""
    assert registry.render_opening_turn(
        "editing", {"project_id": _PROJECT, "base_url": "http://x/"}
    ).text


def test_the_run_rows_link_to_the_runs_they_are_about():
    _record_run(status=RunStatus.AWAITING_REVIEW)
    from app.web.run_index import build_run_index_rows
    rows = [r for r in build_run_index_rows(_PROJECT) if not r.is_test_run]
    queue = build_queue(_PROJECT, rows, read_versions(_PROJECT))
    review = [row for row in queue if row.label == "Review"]
    assert review[0].href.endswith("/runs?status=awaiting_review")
