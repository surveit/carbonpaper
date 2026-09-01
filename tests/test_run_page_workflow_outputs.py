from __future__ import annotations

from pathlib import Path

from app.models.claims import (
    RowsRectangle,
    StageOutputCellCitation,
    StageOutputTableCitation,
)
from app.models.records.workflow_output import WorkflowOutput
from app.web.run_published import (
    PUBLISHED_PREVIEW_ROWS,
    read_published_outputs,
    render_output_value,
)

# The Venezuela figures, as that run really published them.
_PROJECT = "venezuela_lda_lobbying"
_RUN = "20260812T133317.816579"
_STAGE = "count_client_figures"
_NO_FRAMES: dict[str, object] = {"stage_records": []}


def _read(run_id: str = _RUN, run_dir: Path = Path("/nonexistent"), manifest=_NO_FRAMES):
    return read_published_outputs(_PROJECT, run_id, run_dir, manifest)


def _publish(slug: str, label: str, value, run_id: str = _RUN,
             column: str = "external_spend", primary: bool = False):
    WorkflowOutput(
        slug=slug, label=label, primary=primary,
        citation=StageOutputCellCitation(
            run_id=run_id, stage_id=_STAGE, row_ordinal=0,
            column=column, value=value,
        ),
    ).save()


_COLUMNS = ["client", "external_spend"]


def _publish_table(slug: str, label: str, row_count: int, run_id: str = _RUN,
                   primary: bool = False, columns: list[str] = _COLUMNS):
    WorkflowOutput(
        slug=slug, label=label, primary=primary,
        citation=StageOutputTableCitation(
            run_id=run_id, stage_id=_STAGE,
            rectangle=RowsRectangle(row_start=0, row_end=row_count, columns=columns),
        ),
    ).save()


def test_a_runs_outputs_read_back_in_slug_order():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    _publish("clients-paying", "Paying clients", 24, column="clients_paying")
    assert [o.slug for o in _read().figures] == ["clients-paying", "external-spend"]


def test_an_output_links_to_the_cell_it_was_read_from():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    [output] = _read().figures
    assert output.href == (
        f"/project/{_PROJECT}/runs/{_RUN}/stage/{_STAGE}/row/0/trace/view"
        "?column=external_spend"
    )


def test_two_outputs_off_one_row_link_to_their_own_columns():
    # Both figures are row 0 of the same stage, so only the column tells them apart.
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    _publish("clients-paying", "Paying clients", 24, column="clients_paying")
    assert [o.href.rsplit("?", 1)[1] for o in _read().figures] == [
        "column=clients_paying", "column=external_spend",
    ]


def test_another_runs_outputs_are_not_this_runs():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    _publish("external-spend", "Paid to outside firms", 5000000.0, run_id="20260806T163146")
    assert [o.value for o in _read().figures] == ["4,461,000.0"]


def test_a_run_that_published_nothing_shows_nothing():
    published = _read(run_id="20260101T000000")
    assert not published and published.figures == [] and published.tables == []


def test_a_number_reads_with_group_marks():
    assert render_output_value(4461000.0) == "4,461,000.0"
    assert render_output_value(24) == "24"


def test_an_absent_value_reads_as_absent_rather_than_none():
    assert render_output_value(None) == "—"


def test_a_primary_output_is_marked_so_the_page_can_lead_with_it():
    _publish("external-spend", "Paid to outside firms", 4461000.0, primary=True)
    _publish("clients-paying", "Paying clients", 24, column="clients_paying")
    by_slug = {o.slug: o.primary for o in _read().figures}
    assert by_slug == {"external-spend": True, "clients-paying": False}


def test_nothing_is_primary_unless_the_stage_says_so():
    _publish("external-spend", "Paid to outside firms", 4461000.0)
    assert [o.primary for o in _read().figures] == [False]


# ─── Published tables ────────────────────────────────────────────────────────

def test_a_published_table_reads_back_with_its_row_count_and_its_links():
    _publish_table("client-spend", "What each client paid", 24)
    [table] = _read().tables
    assert (table.label, table.row_count) == ("What each client paid", 24)
    # Both links carry the rectangle, so they open what was published, not the frame.
    rows = f"/project/{_PROJECT}/runs/{_RUN}/stage/{_STAGE}/rows"
    query = "rows=0%3A24&columns=client&columns=external_spend"
    assert table.rows_url == f"{rows}?{query}"
    assert table.csv_url == f"{rows}.csv?{query}"


def test_a_table_is_not_read_as_a_figure():
    _publish_table("client-spend", "What each client paid", 24)
    assert _read().figures == []


def test_the_primary_table_is_drawn_first():
    _publish_table("a-secondary", "Secondary", 3)
    _publish_table("z-primary", "Primary", 9, primary=True)
    assert [t.slug for t in _read().tables] == ["z-primary", "a-secondary"]


def test_a_secondary_table_is_a_line_and_a_link_rather_than_a_preview():
    _publish_table("client-spend", "What each client paid", 24)
    assert _read().tables[0].preview is None


def test_a_primary_table_whose_frame_is_gone_still_reads_back():
    # The record says what was published; the frame it previews may be pruned.
    _publish_table("client-spend", "What each client paid", 24, primary=True)
    [table] = _read().tables
    assert table.row_count == 24 and table.preview is None


def _write_frame(run_dir: Path, rows: int) -> dict[str, object]:
    """A run dir holding the stage's output, and the manifest that points at it."""
    import pandas as pd

    from app.core.frames import write_frame_file

    outputs = run_dir / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    write_frame_file(
        pd.DataFrame({"client": [f"client {n}" for n in range(rows)],
                      "external_spend": [1000.0 * n for n in range(rows)],
                      "working_note": ["scratch"] * rows}),
        outputs / f"{_STAGE}.parquet",
    )
    return {"stage_records": [
        {"stage_id": _STAGE, "output_path": f"outputs/{_STAGE}.parquet"}
    ]}


def test_a_primary_table_is_drawn_from_the_frame_the_run_wrote(tmp_path):
    manifest = _write_frame(tmp_path, rows=9)
    _publish_table("client-spend", "What each client paid", 9, primary=True)
    [table] = _read(run_dir=tmp_path, manifest=manifest).tables
    assert table.preview.columns == ["client", "external_spend"]
    assert [c.text for c in table.preview.rows[0].cells] == ["client 0", "0.0"]


def test_a_drawn_table_holds_the_columns_cited_and_no_others(tmp_path):
    # The frame carries a working column; the published table cited two.
    manifest = _write_frame(tmp_path, rows=3)
    _publish_table("client-spend", "What each client paid", 3, primary=True,
                   columns=["client"])
    [table] = _read(run_dir=tmp_path, manifest=manifest).tables
    assert table.preview.columns == ["client"]
    assert [c.text for c in table.preview.rows[0].cells] == ["client 0"]


def test_a_drawn_table_shows_the_first_rows_only(tmp_path):
    manifest = _write_frame(tmp_path, rows=40)
    _publish_table("client-spend", "What each client paid", 40, primary=True)
    [table] = _read(run_dir=tmp_path, manifest=manifest).tables
    assert len(table.preview.rows) == PUBLISHED_PREVIEW_ROWS


def test_every_drawn_cell_opens_its_own_lineage(tmp_path):
    manifest = _write_frame(tmp_path, rows=3)
    _publish_table("client-spend", "What each client paid", 3, primary=True)
    [table] = _read(run_dir=tmp_path, manifest=manifest).tables
    base = f"/project/{_PROJECT}/runs/{_RUN}/stage/{_STAGE}/row"
    assert [c.href for c in table.preview.rows[1].cells] == [
        f"{base}/1/trace/view?column=client",
        f"{base}/1/trace/view?column=external_spend",
    ]


def test_a_cell_keeps_its_own_row_ordinal_not_its_place_in_the_preview(tmp_path):
    manifest = _write_frame(tmp_path, rows=40)
    _publish_table("client-spend", "What each client paid", 40, primary=True)
    [table] = _read(run_dir=tmp_path, manifest=manifest).tables
    assert "/row/4/trace/view" in table.preview.rows[4].cells[0].href
