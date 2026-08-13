"""The one tool the selector is given: search a step's real input rows, and read back
what the run holds. Bound to one step's row sources, so it can reach no other data.
"""
from __future__ import annotations

from typing import Annotated

from app.core.agent.bound_tool import BoundToolSpec
from app.models.schema import StageId
from app.core.row_search import MAX_MATCHES, InputRows, RowMatches

FIND_ROWS_TOOL = "find_rows"

FIND_ROWS_DESCRIPTION = f"""\
Search the real rows this step reads, and pick the ones your examples feed in.
Returns the rows a filter selects — each with a `row` number you quote back when you
submit the case — plus `matched`, how many rows in the whole input the filter
selected, and `scanned`, how many it read.

`matched` is the number that decides whether a case is worth submitting. A filter
matching most of what it scanned has not found a row that exercises anything: it has
found an ordinary row and attached your description of a corner case to it. Narrow it
until the count is small, then read the rows that came back and choose the one you can
say something true about.

`filter` is one expression, in the same dialect a step's own filter is written in:

  income IS NULL
  amount > 1000 AND status == 'open'
  memo.str.contains('[0-9]{{4}}')
  NOT client.str.startswith('The ')

A column, a comparison, IS NULL / IS NOT NULL, AND / OR / NOT, and on a text column
`.str.contains` / `.str.startswith` / `.str.endswith` / `.str.match` / `.str.fullmatch`
and the `.str.is*` tests. `contains`, `match` and `fullmatch` read their argument as a
regular expression; lookahead and backreferences are not searchable and are refused.
Arithmetic is not part of the dialect, so compare a column, do not compute one.

At most {MAX_MATCHES} rows come back per call; the counts are over everything."""


def build_find_rows_tool(sources: dict[StageId, InputRows]) -> BoundToolSpec:
    def find_rows(input: str, filter: str) -> RowMatches:
        source = sources.get(input)
        if source is None:
            raise ValueError(
                f"this step does not read `{input}` — it reads {sorted(sources)}"
            )
        return source.search(filter)

    return BoundToolSpec(
        name=FIND_ROWS_TOOL,
        description=FIND_ROWS_DESCRIPTION,
        fn=find_rows,
        input_schema={
            "input": Annotated[str, "Which input to search, by its stage id."],
            "filter": Annotated[str, "The filter expression, in the dialect above."],
        },
        label="Searching the real rows",
    )
