"""Searching one frame with the filter dialect, and reading a row back out of it.

A `row` here is a POSITION in the frame, which is what names the same row again later.
Who supplies the frame, and which run it came from, is the caller's business.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
from pydantic import BaseModel

from app.core.column_profile import TableProfile
from app.core.frames import convert_row_to_json_cells, list_rows
from app.core.predicate import ParsedPredicate, parse_predicate

# One search's ceiling. A reader of the answer reads every row it holds, so a wide
# answer costs the context the rest of the work needs; narrow the filter instead.
MAX_MATCHES = 20

# The `.str` methods whose first argument pandas reads as a regular expression.
_REGEX_METHODS = frozenset({"contains", "match", "fullmatch"})


class MatchedRow(BaseModel):
    row: int
    values: dict[str, Any]


# `matched` is every row the filter selected; `rows` is the first MAX_MATCHES of them.
class RowMatches(BaseModel):
    input: str
    filter: str
    matched: int
    scanned: int
    rows: list[MatchedRow]


@dataclass(frozen=True)
class InputRows:
    input_id: str
    run_id: str
    frame: pd.DataFrame
    profile: TableProfile

    def find_matching_rows(self, filter_expr: str) -> list[int]:
        parsed = self._parse_against_the_frame(filter_expr)
        mask = self.frame.eval(parsed.pandas_expr)
        if not isinstance(mask, pd.Series):
            raise ValueError(
                f"`{filter_expr}` is one value, not a test each row passes or fails — "
                f"compare a column against something"
            )
        return [int(row) for row in self.frame.index[mask.to_numpy(dtype=bool)]]

    def search(self, filter_expr: str, limit: int = MAX_MATCHES) -> RowMatches:
        matching = self.find_matching_rows(filter_expr)
        shown = matching[:max(1, min(limit, MAX_MATCHES))]
        return RowMatches(
            input=self.input_id,
            filter=filter_expr,
            matched=len(matching),
            scanned=len(self.frame),
            rows=[MatchedRow(row=row, values=self.read_row(row)) for row in shown],
        )

    def read_row(self, row: int) -> dict[str, Any]:
        if row < 0 or row >= len(self.frame):
            raise ValueError(
                f"input `{self.input_id}` of run {self.run_id} holds rows 0 to "
                f"{len(self.frame) - 1}, so there is no row {row}"
            )
        return convert_row_to_json_cells(list_rows(self.frame.iloc[[row]])[0])

    def _parse_against_the_frame(self, filter_expr: str) -> ParsedPredicate:
        parsed = parse_predicate(filter_expr)
        held = {str(name) for name in self.frame.columns}
        unknown = sorted(parsed.columns - held)
        if unknown:
            raise ValueError(
                f"the filter reads {unknown}, which this step does not — it reads "
                f"{sorted(held)}"
            )
        refuse_a_pattern_the_scan_cannot_run(parsed)
        return parsed


def refuse_a_pattern_the_scan_cannot_run(parsed: ParsedPredicate) -> None:
    """A pattern RE2 refuses drops pandas onto a backtracking engine — see #620."""
    for pattern in _find_regex_arguments(parsed.pandas_expr):
        try:
            # One row, not an empty array: with nothing to match, RE2 is never
            # asked to compile the pattern and every pattern looks valid.
            pc.match_substring_regex(pa.array([""]), pattern)
        except pa.ArrowInvalid as exc:
            raise ValueError(
                f"the pattern {pattern!r} cannot be searched for: {exc}"
            ) from exc


def _find_regex_arguments(pandas_expr: str) -> list[str]:
    return [
        node.args[0].value
        for node in ast.walk(ast.parse(pandas_expr, mode="eval"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _REGEX_METHODS
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
