"""Seeds: user-named corpus rows with a required pipeline outcome.

A *seed* here is one input-corpus row the user asserts the pipeline MUST flag
(`must_catch`) or MUST NOT flag (`must_not_catch`). It is the authoring-loop's
ground truth, keyed to a row of the input corpus by that corpus's key column.

(Unrelated to the `app.seeds` package, which holds committed example PROJECTS.)
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class SeedOutcome(str, Enum):
    """What the user asserts the pipeline should do with the seeded corpus row."""

    MUST_CATCH = "must_catch"
    MUST_NOT_CATCH = "must_not_catch"


class SeedRow(BaseModel):
    """One seeded corpus row and its required outcome.

    `row_key` is the value of the corpus key column identifying the row.
    `row_content_hash` is the sha1[:16] of the row's canonical content at
    recording time — stamped by `record_seeds` from the live corpus — so a later
    edit to that corpus row can be detected as stale."""

    row_key: str
    outcome: SeedOutcome
    note: str | None = None
    row_content_hash: str
