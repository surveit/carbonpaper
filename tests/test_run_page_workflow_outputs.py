from __future__ import annotations

from app.models.claims import StageOutputCellCitation
from app.models.records.workflow_output import WorkflowOutput
from app.web.run_header import read_workflow_outputs, render_output_value

# The Venezuela figures, as that run really published them.
_PROJECT = "venezuela_lda_lobbying"
_RUN = "20260812T133317.816579"


def _publish(slug: str, label: str, value, run_id: str = _RUN,
             column: str = "external_spend", primary: bool = False):
    WorkflowOutput(
        slug=slug, label=label, primary=primary,
        citation=StageOutputCellCitation(
            run_id=run_id, stage_id="count_client_figures", row_ordinal=0,
            column=column, value=value,
        ),
    ).save()


def test_a_runs_outputs_read_back_in_slug_order():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    _publish("clients-paying", "Paying clients", 24, column="clients_paying")
    assert [o.slug for o in read_workflow_outputs(_PROJECT, _RUN)] == [
        "clients-paying", "external-spend",
    ]


def test_an_output_links_to_the_row_it_was_read_from():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    [output] = read_workflow_outputs(_PROJECT, _RUN)
    assert output.href == (
        f"/project/{_PROJECT}/runs/{_RUN}/stage/count_client_figures/row/0/trace/view"
    )


def test_another_runs_outputs_are_not_this_runs():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    _publish("external-spend", "Paid to outside firms", 5000000.0, run_id="20260806T163146")
    assert [o.value for o in read_workflow_outputs(_PROJECT, _RUN)] == ["4,461,000.0"]


def test_a_run_that_published_nothing_shows_nothing():
    assert read_workflow_outputs(_PROJECT, "20260101T000000") == []


def test_a_number_reads_with_thousands_separators():
    assert render_output_value(4461000.0) == "4,461,000.0"
    assert render_output_value(24) == "24"


def test_an_absent_value_reads_as_absent_rather_than_none():
    assert render_output_value(None) == "—"


def test_a_primary_output_is_marked_so_the_page_can_lead_with_it():
    _publish("external-spend", "Paid to outside firms", 4461000.0, primary=True)
    _publish("clients-paying", "Paying clients", 24, column="clients_paying")
    by_slug = {o.slug: o.primary for o in read_workflow_outputs(_PROJECT, _RUN)}
    assert by_slug == {"external-spend": True, "clients-paying": False}


def test_nothing_is_primary_unless_the_stage_says_so():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    assert [o.primary for o in read_workflow_outputs(_PROJECT, _RUN)] == [False]
