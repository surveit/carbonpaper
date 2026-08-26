"""A twelve-row workflow hitting every branch origin. Every number checks by hand."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# G-004 is in both sources; G-009's agency is not in the reference; G-007 is a 0.
EAST = [
    ("G-001", "east", "AGENCY-A", 100, "grant"),
    ("G-002", "east", "AGENCY-A", 200, "grant"),
    ("G-003", "east", "AGENCY-B", 300, "loan"),
    ("G-004", "east", "AGENCY-B", 400, "grant"),
    ("G-007", "east", "AGENCY-C", 0, "grant"),
    ("G-009", "east", "AGENCY-Z", 900, "grant"),
]
WEST = [
    ("G-004", "west", "AGENCY-B", 400, "grant"),
    ("G-005", "west", "AGENCY-C", 500, "loan"),
    ("G-006", "west", "AGENCY-C", 600, "grant"),
    ("G-008", "west", "AGENCY-A", 800, "loan"),
]
AGENCIES = [("AGENCY-A", "Health"), ("AGENCY-B", "Transport"), ("AGENCY-C", "Health")]
GRANT_COLUMNS = ["grant_id", "region", "agency_code", "amount", "kind"]


def write_inputs(data: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(EAST, columns=GRANT_COLUMNS).to_csv(data / "east.csv", index=False)
    pd.DataFrame(WEST, columns=GRANT_COLUMNS).to_csv(data / "west.csv", index=False)
    pd.DataFrame(AGENCIES, columns=["agency_code", "portfolio"]).to_csv(
        data / "agencies.csv", index=False)


def column(name: str, type_: str, nullable: bool = True) -> dict:
    return {"name": name, "type": type_, "nullable": nullable}


def _grant_columns() -> list[dict]:
    return [column("grant_id", "str", False), column("region", "str", False),
            column("agency_code", "str", False), column("amount", "int", False),
            column("kind", "str", False)]


def _source(stage_id: str, path: Path) -> dict:
    return {
        "id": stage_id, "type": "input_data", "cache": True,
        "description": f"Grants as {path.name} lists them.",
        "connector": {"kind": "file",
                      "params": {"paths": [str(path)], "format": "csv"}},
        "signature": {"form": "replaces", "produces": _grant_columns()},
    }


BAND_CODE = '''
def transform(row):
    amount = row["amount"]
    if amount == 0:
        band = "none"
    elif amount < 400:
        band = "small"
    else:
        band = "large"
    digits = 0
    written = str(amount)
    for position in range(len(written)):
        if written[position] != "0":
            digits = digits + 1
    return {"band": band, "digits": digits}
'''


def stage_specs(data: Path) -> list[dict]:
    return [
        _source("load_east", data / "east.csv"),
        _source("load_west", data / "west.csv"),
        {
            "id": "load_agencies", "type": "input_data", "cache": True,
            "description": "Which portfolio each agency code belongs to.",
            "connector": {"kind": "file", "params": {
                "paths": [str(data / "agencies.csv")], "format": "csv"}},
            "signature": {"form": "replaces", "produces": [
                column("agency_code", "str", False), column("portfolio", "str", False)]},
        },
        {
            "id": "both_regions", "type": "union", "cache": True,
            "description": "East and west read as one set of ten rows.",
            "inputs": [{"id": "load_east"}, {"id": "load_west"}],
            "signature": {"form": "extends", "reads": [], "adds": [], "rewrites": []},
            "union": {},
        },
        {
            "id": "tag_portfolio", "type": "enrich", "cache": True,
            "description": "Lands each grant's portfolio. AGENCY-Z matches nothing.",
            "inputs": [{"id": "both_regions"}, {"id": "load_agencies"}],
            "join": {"keys": [{"left": "agency_code", "right": "agency_code"}],
                     "enrich_with": {"portfolio": "portfolio"}},
            "signature": {"form": "extends", "reads": [
                {"input": "both_regions",
                 "columns": [column("agency_code", "str", False)]},
                {"input": "load_agencies",
                 "columns": [column("agency_code", "str", False),
                             column("portfolio", "str", False)]}],
                "adds": [column("portfolio", "str")], "rewrites": []},
        },
        {
            "id": "size_band", "type": "starlark_row_function", "cache": True,
            "description": "Bands each grant by amount, and counts its digits.",
            "inputs": [{"id": "tag_portfolio"}],
            "starlark": {
                "summary": "Bands a grant small/medium/large and counts nonzero digits.",
                "corner_cases": [{"case": "amount is 0",
                                  "expected": "band is `none` and digits is 0"}],
                "code": BAND_CODE,
            },
            "signature": {"form": "extends", "reads": [
                {"input": "tag_portfolio", "columns": [column("amount", "int", False)]}],
                "adds": [column("band", "str", False), column("digits", "int", False)],
                "rewrites": []},
        },
        {
            "id": "funded", "type": "filter_rows", "cache": True,
            "description": "Drops the grants recorded at zero.",
            "inputs": [{"id": "size_band"}],
            "filter": {
                "summary": "Keeps a grant only where the recorded amount is above zero.",
                "corner_cases": [{"case": "amount is 0", "expected": "the row is dropped"}],
                "code": 'def should_include(row):\n    return row["amount"] > 0\n',
            },
            "signature": {"form": "extends", "reads": [
                {"input": "size_band", "columns": [column("amount", "int", False)]}],
                "adds": [], "rewrites": []},
        },
        {
            "id": "one_row_per_grant", "type": "dedupe", "cache": True,
            "description": "One row per grant. G-004 was filed in both regions.",
            "inputs": [{"id": "funded"}],
            "dedupe": {"keys": ["grant_id"], "keep": "highest", "by": "amount"},
            "signature": {"form": "extends", "reads": [
                {"input": "funded", "columns": [column("grant_id", "str", False),
                                                column("amount", "int", False)]}],
                "adds": [], "rewrites": []},
        },
        {
            "id": "grants_only", "type": "filter_rows", "cache": True,
            "description": "Keeps the grants, dropping the loans.",
            "inputs": [{"id": "one_row_per_grant"}],
            "filter": {
                "summary": "Keeps a row only where its kind is `grant`.",
                "corner_cases": [{"case": "kind is `loan`",
                                  "expected": "the row is dropped"}],
                "code": 'def should_include(row):\n    return row["kind"] == "grant"\n',
            },
            "signature": {"form": "extends", "reads": [
                {"input": "one_row_per_grant", "columns": [column("kind", "str", False)]}],
                "adds": [], "rewrites": []},
        },
        {
            "id": "grant_totals", "type": "aggregate", "cache": True,
            "description": "One row: what the grants come to.",
            "inputs": [{"id": "grants_only"}],
            "aggregate": {"group_by": [], "aggregations": [
                {"output_column": "grants", "formula": "count"},
                {"output_column": "total_amount", "formula": "sum",
                 "value_column": "amount"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "grants_only", "columns": [column("amount", "int", False)]}],
                "produces": [column("grants", "int"), column("total_amount", "int")]},
        },
        {
            "id": "by_portfolio", "type": "aggregate", "cache": True,
            "description": "What each portfolio came to. The unmatched agency is null.",
            "inputs": [{"id": "one_row_per_grant"}],
            "aggregate": {"group_by": ["portfolio"], "aggregations": [
                {"output_column": "grants", "formula": "count"},
                {"output_column": "total_amount", "formula": "sum",
                 "value_column": "amount"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "one_row_per_grant", "columns": [
                    column("portfolio", "str"), column("amount", "int", False)]}],
                "produces": [column("portfolio", "str"), column("grants", "int"),
                             column("total_amount", "int")]},
        },
        {
            "id": "mean_by_portfolio", "type": "aggregate", "cache": True,
            "description": "The average grant in each portfolio.",
            "inputs": [{"id": "one_row_per_grant"}],
            "aggregate": {"group_by": ["portfolio"], "aggregations": [
                {"output_column": "mean_amount", "formula": "mean",
                 "value_column": "amount"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "one_row_per_grant", "columns": [
                    column("portfolio", "str"), column("amount", "int", False)]}],
                "produces": [column("portfolio", "str"),
                             column("mean_amount", "float")]},
        },
        {
            "id": "over_a_million", "type": "filter_rows", "cache": True,
            "description": "Keeps the grants above a million. None of these are.",
            "inputs": [{"id": "grants_only"}],
            "filter": {
                "summary": "Keeps a grant only where the recorded amount is above a million.",
                "corner_cases": [{"case": "amount is 400",
                                  "expected": "the row is dropped"}],
                "code": 'def should_include(row):\n    return row["amount"] > 1000000\n',
            },
            "signature": {"form": "extends", "reads": [
                {"input": "grants_only", "columns": [column("amount", "int", False)]}],
                "adds": [], "rewrites": []},
        },
        {
            "id": "million_total", "type": "aggregate", "cache": True,
            "description": "One row: what those come to. No row fed it.",
            "inputs": [{"id": "over_a_million"}],
            "aggregate": {"group_by": [], "aggregations": [
                {"output_column": "total_amount", "formula": "sum",
                 "value_column": "amount"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "over_a_million", "columns": [column("amount", "int", False)]}],
                "produces": [column("total_amount", "int")]},
        },
        {
            "id": "million_total_summed", "type": "aggregate", "cache": True,
            "description": "Sums a total that no row fed.",
            "inputs": [{"id": "million_total"}],
            "aggregate": {"group_by": [], "aggregations": [
                {"output_column": "summed_total", "formula": "sum",
                 "value_column": "total_amount"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "million_total",
                 "columns": [column("total_amount", "int")]}],
                "produces": [column("summed_total", "int")]},
        },
        {
            "id": "total_of_means", "type": "aggregate", "cache": True,
            "description": "The three portfolio averages added together.",
            "inputs": [{"id": "mean_by_portfolio"}],
            "aggregate": {"group_by": [], "aggregations": [
                {"output_column": "summed_means", "formula": "sum",
                 "value_column": "mean_amount"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "mean_by_portfolio",
                 "columns": [column("mean_amount", "float")]}],
                "produces": [column("summed_means", "float")]},
        },
    ]


def review_tail() -> list[dict]:
    """A halting review stage and one aggregate after it, so a run leaves work pending."""
    return [
        {
            "id": "review_totals", "type": "human_review_queue",
            "description": "A human checks the total before anything downstream reads it.",
            "inputs": [{"id": "grant_totals"}],
            "signature": {"form": "extends", "reads": [
                {"input": "grant_totals", "columns": [
                    column("grants", "int"), column("total_amount", "int")]}],
                "adds": [column("checked_amount", "int"), column("decision", "str"),
                         column("reviewer_id", "str"), column("reviewed_at", "str"),
                         column("review_notes", "str")]},
            "queue": {"reviewed_columns": {"total_amount": "checked_amount"},
                      "verdict_column": "decision", "reviewer_column": "reviewer_id",
                      "reviewed_at_column": "reviewed_at",
                      "review_notes_column": "review_notes"},
        },
        {
            "id": "count_reviewed", "type": "aggregate",
            "description": "What the reviewer's signed-off totals come to.",
            "inputs": [{"id": "review_totals"}],
            "aggregate": {"group_by": [], "aggregations": [
                {"output_column": "reviewed_total", "formula": "sum",
                 "value_column": "checked_amount"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "review_totals", "columns": [column("checked_amount", "int")]}],
                "produces": [column("reviewed_total", "int")]},
        },
    ]


TIER_CODE = '''
def transform(row):
    if row["portfolio"] == "Health":
        tier = "clinical"
    else:
        tier = "built"
    return {"tier": tier}
'''


def give_the_lookup_a_stage_of_its_own(specs: list[dict]) -> list[dict]:
    """The same workflow with a stage between the lookup table and the join that reads it."""
    tiered = []
    for spec in specs:
        if spec["id"] == "tag_portfolio":
            tiered.append(_tier_agencies())
            spec = {**spec, "inputs": [{"id": "both_regions"}, {"id": "tier_agencies"}],
                    "signature": {**spec["signature"], "reads": [
                        {"input": "both_regions",
                         "columns": [column("agency_code", "str", False)]},
                        {"input": "tier_agencies",
                         "columns": [column("agency_code", "str", False),
                                     column("portfolio", "str", False)]}]}}
        tiered.append(spec)
    return tiered


def _tier_agencies() -> dict:
    return {
        "id": "tier_agencies", "type": "starlark_row_function", "cache": True,
        "description": "Tiers a portfolio. AGENCY-B's Transport takes the else arm.",
        "inputs": [{"id": "load_agencies"}],
        "starlark": {
            "summary": "Tiers a portfolio clinical where it is Health, else built.",
            "corner_cases": [{"case": "portfolio is Transport",
                              "expected": "tier is `built`"}],
            "code": TIER_CODE,
        },
        "signature": {"form": "extends", "reads": [
            {"input": "load_agencies",
             "columns": [column("portfolio", "str", False)]}],
            "adds": [column("tier", "str", False)], "rewrites": []},
    }


PUBLISH_CODE = '''
def transform(df, output_dir, citation_provider):
    url = citation_provider.cite_value(
        "grant_totals", 0, "total_amount", df["total_amount"].iloc[0],
        label="What the grants come to")
    with open(output_dir + "/report.html", "w", encoding="utf-8") as report:
        report.write("<p>" + str(df["total_amount"].iloc[0]) + " " + url + "</p>")
    return []
'''


def publish_tail() -> list[dict]:
    """A publish stage citing the total, so the run records one published figure."""
    return [
        {
            "id": "publish_totals", "type": "publish",
            "description": "Publish the total, cited back to the cell it came from.",
            "inputs": [{"id": "grant_totals"}],
            "publish": {"format": "html_report"},
            "function": {
                "kind": "inline",
                "summary": "Writes one paragraph: the total and a link to its rows.",
                "code": PUBLISH_CODE,
            },
            "signature": {"form": "replaces", "reads": [
                {"input": "grant_totals",
                 "columns": [column("total_amount", "int")]}], "produces": []},
        },
    ]
