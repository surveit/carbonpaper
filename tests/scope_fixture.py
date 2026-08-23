"""A twelve-row workflow that exercises every branch origin and the corners a real
project rarely reaches. Every number in the tests below is checkable by hand."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# G-004 is in BOTH sources: the dedupe, so an aggregate downstream merges two rows
# that disagree about which source they came from.
# G-009's agency is absent from the reference: the lookup miss.
# G-007 is recorded at 0: the filter drops it.
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
