"""The told figure: which stages speak, in what words, and what a count hovers on."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.models.row_types import RowType
from app.models.supported_phrases import PhraseStyle, say_share
from app.models.supported_statement import (
    ClauseKind,
    FigureStep,
    StepRows,
    build_supported_statement,
)

FILING_RECORD = RowType(id="filing_record", title="Filing record",
                        definition="One filing as it appeared in one quarterly export.")
FILING = RowType(id="filing", title="Filing",
                 definition="One lobbying filing, stating its money once.")


def _column(name, type_="str"):
    return {"name": name, "type": type_, "nullable": True}


def _load(stage_id, columns):
    """`columns` is {name: type}, so an aggregation over one of them declares a real type."""
    return {
        "id": stage_id, "description": f"Load {stage_id}", "type": "input_data",
        "connector": {"kind": "file", "params": {"paths": [f"/in/{stage_id}.csv"]}},
        "signature": {"form": "replaces",
                      "produces": [_column(name, type_) for name, type_ in columns.items()]},
    }


def _filter(stage_id, source, reads, predicate=None):
    """`reads` is {name: type}: a read whose type differs from the producer's is refused."""
    block = {"code": "def should_include(row): return True"}
    if predicate is not None:
        block["predicate"] = predicate
    return {
        "id": stage_id, "description": f"Keep some of {source}", "type": "filter_rows",
        "inputs": [{"id": source}], "filter": block,
        "signature": {"form": "extends",
                      "reads": [{"input": source,
                                 "columns": [_column(name, type_)
                                             for name, type_ in reads.items()]}]},
    }


def _row_function(stage_id, source, reads, adds=()):
    return {
        "id": stage_id, "description": f"Rewrite {source}",
        "type": "python_row_function", "inputs": [{"id": source}],
        "function": {"kind": "inline", "code": "def transform(row): return row"},
        "signature": {"form": "extends",
                      "reads": [{"input": source,
                                 "columns": [_column(name, type_)
                                             for name, type_ in reads.items()]}],
                      "adds": [_column(name, "str") for name in adds]},
    }


_AGGREGATED_TYPES = {"sum": "float", "mean": "float", "count": "int"}


def _aggregate(stage_id, source, group_by, aggregations, columns):
    """Reads and produces follow the config, which is what the stage's own checks demand."""
    read = list(dict.fromkeys(group_by + [op["value_column"] for op in aggregations
                                          if op.get("value_column")]))
    produces = ([_column(name, columns[name]) for name in group_by]
                + [_column(op["output_column"], _aggregated_type(op, columns))
                   for op in aggregations])
    return {
        "id": stage_id, "description": f"Group {source}", "type": "aggregate",
        "inputs": [{"id": source}],
        "aggregate": {"group_by": group_by, "aggregations": aggregations},
        "signature": {"form": "replaces",
                      "reads": [{"input": source,
                                 "columns": [_column(name, columns[name])
                                             for name in read]}],
                      "produces": produces},
    }


def _aggregated_type(op, columns):
    formula = op["formula"]
    if formula in _AGGREGATED_TYPES:
        return _AGGREGATED_TYPES[formula]
    source = columns[op["value_column"]]
    return f"list[{source}]" if formula == "list" else source


def _hold(specs, rows, row_types):
    """One FigureStep per spec, in the order given, which is the walk's order."""
    stages = m.Workflow(
        stages=[m.parse_stage(spec) for spec in specs]).index_workflow_stages_by_id()
    return [FigureStep(stage=stages[spec["id"]], rows=rows[spec["id"]],
                       row_type=row_types.get(spec["id"]))
            for spec in specs]


def _read(statement):
    return [" ".join("".join(phrase.text for phrase in clause.phrases)
                     for clause in paragraph)
            for paragraph in statement.paragraphs]


def _hovers(statement):
    return {phrase.text: phrase.hover
            for paragraph in statement.paragraphs for clause in paragraph
            for phrase in clause.phrases if phrase.hover}


FILING_COLUMNS = {"income_usd": "float", "filing_uuid": "str"}
MONEY_READ = {"income_usd": "float"}

ROUTE = [
    _load("input_filings", FILING_COLUMNS),
    _row_function("find_ai_mentions", "input_filings", MONEY_READ),
    _filter("keep_ai_candidates", "find_ai_mentions", MONEY_READ,
            predicate="use one of six AI terms in their issue text"),
    _filter("keep_ai_lobbying", "keep_ai_candidates", MONEY_READ,
            predicate="a model read as really about AI"),
    _aggregate("ai_filings", "keep_ai_lobbying", ["filing_uuid"],
               [{"output_column": "income_usd", "formula": "only",
                 "value_column": "income_usd"},
                {"output_column": "uuids_as_filed", "formula": "list",
                 "value_column": "filing_uuid"}],
               FILING_COLUMNS),
    _filter("select_external_filings", "ai_filings", MONEY_READ,
            predicate="are one organisation paying another to lobby"),
    _aggregate("ai_spend_totals", "select_external_filings", [],
               [{"output_column": "total_income_usd", "formula": "sum",
                 "value_column": "income_usd"}],
               FILING_COLUMNS),
]

ROWS = {
    "input_filings": StepRows(rows_out=45061, rows_dropped=0, rows_behind=1294,
                              columns_behind=["income_usd", "filing_uuid"]),
    "find_ai_mentions": StepRows(rows_out=45061, rows_dropped=0, rows_behind=1294),
    "keep_ai_candidates": StepRows(rows_out=2139, rows_dropped=42922, rows_behind=1294),
    "keep_ai_lobbying": StepRows(rows_out=2065, rows_dropped=74, rows_behind=1294),
    "ai_filings": StepRows(rows_out=1975, rows_dropped=0, rows_behind=1292),
    "select_external_filings": StepRows(rows_out=1295, rows_dropped=680,
                                        rows_behind=1292),
    "ai_spend_totals": StepRows(rows_out=1, rows_dropped=0, rows_behind=1),
}

NOUNS = {"input_filings": FILING_RECORD, "find_ai_mentions": FILING_RECORD,
         "keep_ai_candidates": FILING_RECORD, "keep_ai_lobbying": FILING_RECORD,
         "ai_filings": FILING, "select_external_filings": FILING}


@pytest.fixture
def told():
    return build_supported_statement(_hold(ROUTE, ROWS, NOUNS), "total_income_usd")


def test_a_regrain_closes_its_paragraph_and_the_next_one_names_its_own_noun(told):
    assert _read(told) == [
        "The run loads 45,061 filing records."
        " The figure reads income_usd, filing_uuid."
        " Of those filing records, only the ones that use one of six AI terms in their"
        " issue text go on."
        " Of those, the ones that a model read as really about AI remain."
        " What survives is held to one row per filing_uuid, the duplicates having to"
        " agree, and becomes filings.",
        "Of those filings, only the ones that are one organisation paying another to"
        " lobby go on."
        " Their income_usd, summed, is the figure.",
    ]


def test_a_stage_that_wrote_no_column_and_took_no_row_says_nothing(told):
    assert "find_ai_mentions" not in [clause.stage_id for paragraph in told.paragraphs
                                      for clause in paragraph]


def test_a_stage_that_wrote_on_every_row_says_what_it_wrote():
    route = [_load("input_filings", FILING_COLUMNS),
             _row_function("find_ai_mentions", "input_filings", MONEY_READ,
                           adds=["mentions_ai", "ai_terms_found"]),
             _aggregate("totals", "find_ai_mentions", [],
                        [{"output_column": "total", "formula": "sum",
                          "value_column": "income_usd"}], FILING_COLUMNS)]
    rows = {"input_filings": ROWS["input_filings"],
            "find_ai_mentions": StepRows(rows_out=45061, rows_dropped=0,
                                         rows_behind=1294),
            "totals": StepRows(rows_out=1, rows_dropped=0, rows_behind=1)}
    statement = build_supported_statement(_hold(route, rows, NOUNS), "total")
    assert "Each is given mentions_ai and ai_terms_found." in _read(statement)[0]


def test_a_clause_that_says_only_a_name_asks_for_the_step_s_own_line(told):
    route = [_load("input_filings", FILING_COLUMNS),
             _filter("keep_ai_candidates", "input_filings", MONEY_READ),
             _aggregate("totals", "keep_ai_candidates", [],
                        [{"output_column": "total", "formula": "sum",
                          "value_column": "income_usd"}], FILING_COLUMNS)]
    rows = {"input_filings": ROWS["input_filings"],
            "keep_ai_candidates": ROWS["keep_ai_candidates"],
            "totals": StepRows(rows_out=1, rows_dropped=0, rows_behind=1)}
    unwritten = build_supported_statement(_hold(route, rows, NOUNS), "total")
    said = unwritten.paragraphs[0][1]
    assert said.needs_the_description is True
    assert said.description == "Keep some of input_filings"
    # The same filter with a predicate says its own meaning, so it asks for nothing.
    written = {clause.stage_id: clause for paragraph in told.paragraphs
               for clause in paragraph}["keep_ai_candidates"]
    assert (written.needs_the_description, written.description) == (
        False, "Keep some of find_ai_mentions")


def test_every_clause_names_the_stage_it_opens(told):
    assert [clause.stage_id for paragraph in told.paragraphs
            for clause in paragraph] == [
        "input_filings", "keep_ai_candidates", "keep_ai_lobbying", "ai_filings",
        "select_external_filings", "ai_spend_totals"]


def test_a_narrowing_hovers_its_two_counts_and_the_share_that_went_on(told):
    assert (_hovers(told)["that use one of six AI terms in their issue text"]
            == "2,139 of 45,061 go on · 4.7%")


def test_the_two_counts_the_page_prints_are_the_ends_of_the_route(told):
    printed = [phrase.text for paragraph in told.paragraphs for clause in paragraph
               for phrase in clause.phrases if phrase.style is PhraseStyle.count_]
    assert printed == ["45,061 filing records", "filings"]


def test_the_population_hovers_the_row_type_and_what_the_figure_rests_on(told):
    assert _hovers(told)["45,061 filing records"] == (
        "One filing as it appeared in one quarterly export. 45,061 filing records "
        "here, 1,294 of them behind this figure.")


def test_a_filter_nobody_wrote_a_predicate_for_is_named_by_its_stage():
    route = [_load("input_filings", FILING_COLUMNS),
             _filter("keep_ai_candidates", "input_filings", MONEY_READ),
             _aggregate("totals", "keep_ai_candidates", [],
                        [{"output_column": "total", "formula": "sum",
                          "value_column": "income_usd"}], FILING_COLUMNS)]
    rows = {"input_filings": ROWS["input_filings"],
            "keep_ai_candidates": ROWS["keep_ai_candidates"],
            "totals": StepRows(rows_out=1, rows_dropped=0, rows_behind=1)}
    statement = build_supported_statement(_hold(route, rows, NOUNS), "total")
    assert ("Of those filing records, only the ones keep_ai_candidates kept go on."
            in _read(statement)[0])


def test_a_step_that_dropped_nothing_says_so_rather_than_narrowing():
    route = [_load("input_filings", FILING_COLUMNS),
             _filter("keep_everything", "input_filings", MONEY_READ,
                     predicate="carry an income"),
             _aggregate("totals", "keep_everything", [],
                        [{"output_column": "total", "formula": "sum",
                          "value_column": "income_usd"}], FILING_COLUMNS)]
    rows = {"input_filings": ROWS["input_filings"],
            "keep_everything": StepRows(rows_out=45061, rows_dropped=0,
                                        rows_behind=1294),
            "totals": StepRows(rows_out=1, rows_dropped=0, rows_behind=1)}
    statement = build_supported_statement(_hold(route, rows, NOUNS), "total")
    assert "Every one that carry an income, so the step narrowed nothing." in _read(
        statement)[0]


def test_a_grouping_that_combines_values_is_told_as_a_gather_not_an_assert():
    columns = {"value": "float", "iso3": "str", "year_int": "int",
               "income_group": "str"}
    route = [_load("generation", columns),
             _aggregate("country_year", "generation", ["iso3", "year_int"],
                        [{"output_column": "value", "formula": "sum",
                          "value_column": "value"}], columns),
             _aggregate("by_income_group", "country_year", ["iso3"],
                        [{"output_column": "mean_share", "formula": "mean",
                          "value_column": "value"}], columns)]
    rows = {"generation": StepRows(rows_out=37403, rows_dropped=0, rows_behind=286,
                                   columns_behind=["value"]),
            "country_year": StepRows(rows_out=6132, rows_dropped=0, rows_behind=286),
            "by_income_group": StepRows(rows_out=139, rows_dropped=0, rows_behind=1)}
    statement = build_supported_statement(_hold(route, rows, {}), "mean_share")
    assert _read(statement) == [
        "The run loads 37,403 rows. The figure reads value."
        " These are gathered into one row per iso3 and year_int, their values summed.",
        "Their value is averaged for each iso3 — 139 rows in all — and that is"
        " the figure.",
    ]


def test_the_figure_stage_is_the_head_however_it_would_otherwise_read():
    route = [_load("input_filings", FILING_COLUMNS),
             _filter("confirm", "input_filings", MONEY_READ,
                     predicate="a reviewer left standing")]
    rows = {"input_filings": ROWS["input_filings"],
            "confirm": StepRows(rows_out=1292, rows_dropped=3, rows_behind=1292)}
    statement = build_supported_statement(_hold(route, rows, NOUNS), "income_usd")
    assert statement.paragraphs[0][-1].kind is ClauseKind.head
    assert _read(statement)[0].endswith(
        "Their income_usd, as confirm wrote it, is the figure.")


def test_a_share_never_rounds_to_all_of_them_while_a_row_was_dropped():
    assert say_share(1292, 1295) == "99.8%"
    assert say_share(45060, 45061) == "over 99.9%"
    assert say_share(1295, 1295) == "100%"
    assert say_share(1, 45061) == "under 1%"


def test_a_share_of_nothing_is_refused_rather_than_divided():
    with pytest.raises(ValueError, match="no share of 0 rows"):
        say_share(0, 0)


def test_a_predicate_longer_than_a_phrase_is_refused():
    with pytest.raises(ValidationError, match="predicate"):
        m.parse_stage(_filter("keep", "load", MONEY_READ, predicate="x" * 121))
