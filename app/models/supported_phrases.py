"""The spans a told figure is made of, and the numbers and nouns they carry."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from app.models.row_types import RowType

# What a reader is told when a column carries no description of its own.
NO_COLUMN_DESCRIPTION = "This column carries no description."
# The word for one row where nothing in the stage's ancestry named a row type.
UNNAMED_ROWS = "rows"


class PhraseStyle(str, Enum):
    prose = "prose"
    # A count and its noun: the two the page prints.
    count_ = "count"  # trailing underscore: `count` would shadow str.count
    column = "column"
    # A stage's own id, said where nobody wrote what the step means.
    name = "name"


class Phrase(BaseModel):
    text: str
    style: PhraseStyle = PhraseStyle.prose
    # Empty where the span is plain prose; otherwise what the reader reads on hover.
    hover: str = ""


def text_phrase(text: str) -> Phrase:
    return Phrase(text=text)


def count_phrase(text: str, hover: str) -> Phrase:
    return Phrase(text=text, style=PhraseStyle.count_, hover=hover)


def column_phrase(name: str, description: Optional[str]) -> Phrase:
    return Phrase(text=name, style=PhraseStyle.column,
                  hover=description or NO_COLUMN_DESCRIPTION)


def name_phrase(stage_id: str, hover: str) -> Phrase:
    return Phrase(text=stage_id, style=PhraseStyle.name, hover=hover)


# Grouped from the first thousand: every number here is a count, never a year.
def say_count(rows: int) -> str:
    return format(rows, ",")


def say_share(part: int, whole: int) -> str:
    """Refuses rather than dividing by zero: a step nothing reached has no share to state."""
    if whole <= 0:
        raise ValueError(f"no share of {whole} rows to state")
    share = 100 * part / whole
    if part == whole:
        return "100%"
    # A step that dropped rows must never read 100%, however few it dropped.
    if share > 99.9:
        return "over 99.9%"
    if part and share < 1:
        return "under 1%"
    return f"{share:.0f}%" if 10 <= share < 99.5 else f"{share:.1f}%"


def say_plural(row_type: Optional[RowType]) -> str:
    if row_type is None:
        return UNNAMED_ROWS
    title = row_type.title.lower()
    return title if title.endswith("s") else f"{title}s"


def say_list(phrases: Sequence[Phrase]) -> list[Phrase]:
    return _say_series(phrases, last_join=", ")


def say_and_list(phrases: Sequence[Phrase]) -> list[Phrase]:
    return _say_series(phrases, last_join=" and ")


def _say_series(phrases: Sequence[Phrase], last_join: str) -> list[Phrase]:
    said: list[Phrase] = []
    for position, phrase in enumerate(phrases):
        if position:
            said.append(text_phrase(last_join if position == len(phrases) - 1 else ", "))
        said.append(phrase)
    return said
