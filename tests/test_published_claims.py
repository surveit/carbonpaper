from __future__ import annotations

from app.models.claims import StageOutputCellCitation
from app.models.records.workflow_output import WorkflowOutput
from app.web.panel_links import AppPanelLinks, PacketPanelLinks
from app.web.published_claims import read_published_claims, render_output_value

# The Venezuela figures, as that run really published them.
_PROJECT = "venezuela_lda_lobbying"
_RUN = "20260812T133317.816579"
_STAGE = "count_client_figures"


def _publish(slug: str, label: str, value, run_id: str = _RUN,
             column: str = "external_spend", primary: bool = False):
    WorkflowOutput(
        slug=slug, label=label, primary=primary,
        citation=StageOutputCellCitation(
            run_id=run_id, stage_id=_STAGE, row_ordinal=0,
            column=column, value=value,
        ),
    ).save()


def _app_claims():
    return read_published_claims(_RUN, AppPanelLinks(_PROJECT, _RUN))


def test_a_runs_claims_read_back_in_slug_order():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    _publish("clients-paying", "Paying clients", 24, column="clients_paying")
    assert [claim.label for claim in _app_claims().rest] == [
        "Paying clients", "Paid to outside firms",
    ]


def test_a_claim_links_to_the_cell_it_was_read_from():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    [claim] = _app_claims().rest
    assert claim.href == (
        f"/project/{_PROJECT}/runs/{_RUN}/stage/{_STAGE}/row/0/trace/view"
        "?column=external_spend"
    )


def test_two_claims_off_one_row_link_to_their_own_columns():
    # Both figures are row 0 of the same stage, so only the column tells them apart.
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    _publish("clients-paying", "Paying clients", 24, column="clients_paying")
    assert [c.href.rsplit("?", 1)[1] for c in _app_claims().rest] == [
        "column=clients_paying", "column=external_spend",
    ]


def test_another_runs_claims_are_not_this_runs():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    _publish("external-spend", "Paid to outside firms", 5000000.0, run_id="20260806T163146")
    assert [claim.value for claim in _app_claims().rest] == ["4,461,000.0"]


def test_a_run_that_published_nothing_shows_nothing():
    claims = read_published_claims("20260101T000000", AppPanelLinks(_PROJECT, _RUN))
    assert not claims.any()


def test_a_number_reads_with_thousands_separators():
    assert render_output_value(4461000.0) == "4,461,000.0"
    assert render_output_value(24) == "24"


def test_an_absent_value_reads_as_absent_rather_than_none():
    assert render_output_value(None) == "—"


def test_a_primary_claim_leads_so_the_page_can_open_on_it():
    _publish("external-spend", "Paid to outside firms", 4461000.0, primary=True)
    _publish("clients-paying", "Paying clients", 24, column="clients_paying")
    claims = _app_claims()
    assert [c.label for c in claims.leads] == ["Paid to outside firms"]
    assert [c.label for c in claims.rest] == ["Paying clients"]


def test_nothing_leads_unless_the_stage_says_so():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    assert _app_claims().leads == []


def test_a_packet_claim_opens_the_lineage_page_this_packet_holds():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    links = PacketPanelLinks(to_root="", traced=frozenset({(_STAGE, 0)}))
    claims = read_published_claims(_RUN, links)
    assert [c.href for c in claims.rest] == [f"lineage/{_STAGE}/0.html"]
    assert claims.any_traced()


def test_a_packet_claim_with_no_lineage_page_keeps_its_value_and_loses_its_link():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    links = PacketPanelLinks(to_root="", traced=frozenset())
    claims = read_published_claims(_RUN, links)
    assert [(c.value, c.href) for c in claims.rest] == [("4,461,000.0", None)]
    assert not claims.any_traced()
