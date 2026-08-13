"""`_run_guide.html` as markup: the three parts of a Workflow section, in order,
and what the section's output link is allowed to say. Rendered straight through the
app's Jinja environment over a hand-built view, so nothing here needs a run on disk."""
from __future__ import annotations

import re

import pytest

from app.models import Stage, parse_stage
from app.services.run_guide import GuideStageView, GuideStepView, RunGuideView
from app.web.config import templates
from app.web.panel_links import AppPanelLinks
from conftest import place_stage

_ROWS = [{"name": "doc_id", "type": "str", "nullable": False}]


def _stage(stage_id: str, description: str) -> Stage:
    return parse_stage({
        "id": stage_id, "description": description, "type": "input_data",
        "connector": {"kind": "file"},
        "signature": {"form": "replaces", "produces": _ROWS},
    })


def _stage_view(
    stage_id: str, description: str, *, rows: int | None, columns: int | None,
    executed: bool = True,
) -> GuideStageView:
    return GuideStageView(
        stage_id=stage_id,
        workflow_stage=place_stage(_stage(stage_id, description)),
        written_columns=["doc_id"],
        executed=executed,
        output_row_count=rows,
        column_count=columns,
    )


def _render(
    *steps: GuideStepView,
    published: list[object] | None = None,
) -> str:
    html = templates.get_template("_run_guide.html").render(
        guide=RunGuideView(steps=list(steps), unnarrated=[]),
        project_id="demo",
        published_artifacts=published or [],
        links=AppPanelLinks("demo", "20260101T000000"),
    )
    # The rail carries its own stylesheet, which names every class these tests look
    # for. Only the markup after it is the rendered section.
    return html.split("</style>", 1)[1]


def _section(
    *outputs: GuideStageView,
    stages: list[GuideStageView] | None = None,
    data_description: str | None = None,
) -> GuideStepView:
    return GuideStepView(
        title="Read both quarters and tag them",
        prose="Both quarters are read in and combined.",
        data_description=data_description,
        stages=list(outputs) if stages is None else stages,
        outputs=list(outputs),
    )


def _union() -> GuideStageView:
    return _stage_view(
        "union_filings", "Both quarters as one filing table", rows=45_061, columns=15
    )


# ── the three parts, in order ────────────────────────────────────────────────

def test_a_section_reads_description_then_transforms_then_output() -> None:
    html = _render(_section(_union(), data_description="Every filing both quarters reported."))

    order = [
        html.index("Both quarters are read in and combined."),
        html.index("Transforms"),
        html.index("guide-outputs"),
    ]
    assert order == sorted(order)


def test_the_transforms_are_not_folded_behind_a_disclosure() -> None:
    html = _render(_section(_union()))

    assert "<details" not in html
    assert '<span class="guide-stage-id">union_filings</span>' in html


def test_a_transform_row_is_chipped_with_the_stage_id_not_its_type() -> None:
    html = _render(_section(_union()))

    assert '<span class="guide-stage-id">union_filings</span>' in html
    assert "input_data</span>" not in html


# ── the size: abbreviated on the link, exact on its title ────────────────────

def test_the_link_shows_the_abbreviated_size() -> None:
    html = _render(_section(_union()))

    assert "45.1k" in html
    assert "45,061 rows × 15 columns" in html


def test_the_exact_count_rides_on_the_links_own_title() -> None:
    html = _render(_section(_union()))

    [title] = re.findall(r'<a class="guide-output"[^>]*title="([^"]*)"', html)
    assert title.startswith("45,061 rows × 15 columns")


@pytest.mark.parametrize(
    ("rows", "columns", "expected"),
    [
        (None, 15, "15 columns"),
        (7_400, None, "7.4k rows"),
    ],
)
def test_an_unmeasured_half_says_so_and_is_never_rendered_as_a_zero(
    rows: int | None, columns: int | None, expected: str
) -> None:
    html = _render(_section(_stage_view("s", "S", rows=rows, columns=columns)))

    [size] = re.findall(r'<span class="guide-output-size[^"]*">(.*?)</span>', html, re.S)
    assert size.strip() == expected
    assert "0" not in expected


def test_a_stage_the_run_never_executed_says_so_rather_than_calling_it_unknown() -> None:
    html = _render(_section(
        _stage_view("s", "S", rows=None, columns=None, executed=False)
    ))

    assert '-<span class="guide-output-x">\u00d7</span>-' in html
    [title] = re.findall(r'<a class="guide-output"[^>]*title="([^"]*)"', html)
    assert title.startswith("This data was not produced in this run")
    assert "unknown" not in html


def test_a_stage_that_ran_but_measured_nothing_is_not_called_unexecuted() -> None:
    html = _render(_section(
        _stage_view("s", "S", rows=None, columns=None, executed=True)
    ))

    [title] = re.findall(r'<a class="guide-output"[^>]*title="([^"]*)"', html)
    assert title.startswith("This run produced this data but recorded neither")


def test_the_unmeasured_link_is_not_dressed_as_a_measured_one() -> None:
    html = _render(_section(
        _stage_view("s", "S", rows=None, columns=None, executed=False)
    ))

    assert 'class="guide-output-size unmeasured"' in html


def test_a_measured_empty_frame_still_reads_as_a_zero() -> None:
    html = _render(_section(_stage_view("s", "S", rows=0, columns=3)))

    assert "0 rows × 3 columns" in html


# ── the output group ─────────────────────────────────────────────────────────

def test_one_hairline_covers_every_pathway_out_of_a_section() -> None:
    outputs = [
        _stage_view("select_core_filings", "Keep the paid filings", rows=40, columns=30),
        _stage_view("select_incidental_filings", "Keep the in-house ones", rows=24, columns=30),
        _stage_view("select_registration_filings", "Keep the registrations", rows=34, columns=30),
    ]
    html = _render(_section(*outputs))

    assert html.count('class="guide-outputs"') == 1
    assert html.count('class="guide-output"') == 3


def test_a_forking_section_names_each_branch_it_leaves() -> None:
    outputs = [
        _stage_view("select_core_filings", "Keep the paid filings", rows=40, columns=30),
        _stage_view("select_incidental_filings", "Keep the in-house ones", rows=24, columns=30),
    ]
    html = _render(_section(*outputs, data_description="The candidates, split by kind."))

    assert "select_core_filings" in html
    # One section, one authored sentence: it sits above the branches rather than
    # being repeated against each, which would read as three separate claims.
    assert html.count("The candidates, split by kind.") == 1


def test_a_section_with_no_authored_sentence_gets_its_link_and_no_sentence() -> None:
    html = _render(_section(_union()))

    assert "45.1k" in html
    assert 'class="guide-output-what"' not in html



# ── where the run ended up ───────────────────────────────────────────────────

class _Artifact:
    def __init__(self, name: str, url: str) -> None:
        self.name, self.url = name, url


def test_the_published_files_lead_the_rail_rather_than_trailing_it() -> None:
    html = _render(_section(_union()),
                   published=[_Artifact("filings.xlsx", "/runs/x/artifacts/filings.xlsx")])

    assert html.index("guide-published") < html.index("guide-steps")
    assert "filings.xlsx" in html


def test_a_run_that_published_nothing_shows_no_published_block() -> None:
    assert "guide-published" not in _render(_section(_union()))


