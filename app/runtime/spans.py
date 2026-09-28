"""Checks each span against the file its run read: the quote must sit at its address there."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
from pydantic import ValidationError

from app.core.errors import (
    LocatorKindUnreadable,
    PageOutOfRange,
    QuoteNotAtAddress,
    SourceChanged,
    SourceNotRead,
    UnsupportedTextFormat,
)
from app.core.files import compute_sha256
from app.core.frames import is_null_form, list_table_rows
from app.core.text_sources import normalize_text, read_page_text
from app.core.utils import format_errors
from app.models import SPAN_COLUMN_TYPE, Column, TableSchema
from app.models.locators import PageCharRange, label_locator
from app.models.schema import find_list_element_type
from app.models.severity import UserFacingErrorSeverity
from app.models.spans import Span

from .validation import Issue

_REFUSALS_SHOWN = 3
SPAN_REFUSALS = (
    LocatorKindUnreadable,
    PageOutOfRange,
    QuoteNotAtAddress,
    SourceChanged,
    SourceNotRead,
    UnsupportedTextFormat,
)


def find_span_issues(
    table: pa.Table, schema: TableSchema, sources: Mapping[str, Path], texts: SourceTextCache
) -> list[Issue]:
    """One error per column holding a refused span; a table with no span column reads no file."""
    span_columns = [
        column for column in schema.columns
        if column.name in table.column_names and _holds_spans(column)
    ]
    if not span_columns:
        return []
    rows = list_table_rows(table.select([column.name for column in span_columns]))
    return [
        issue for column in span_columns
        for issue in _find_column_issues(rows, column, sources, texts)
    ]


def verify_span(span: Span, sources: Mapping[str, Path], texts: SourceTextCache) -> None:
    """Raises unless the quote, and any prefix and suffix, sit at the span's address."""
    path = sources.get(span.source_sha256)
    if path is None:
        raise SourceNotRead(
            f"this run read no file with sha256 {span.source_sha256} (source {span.source_id})")
    locator = require_page_locator(span)
    page_text = texts.read_page_text(path, span.source_sha256, locator.page)
    locator.validate_text(page_text)
    where = f"{label_locator(locator)} of {path.name}"
    _require_quote_at(where, locator, page_text, span.quote)
    if span.prefix is not None:
        _require_prefix_at(where, locator, page_text, span.prefix)
    if span.suffix is not None:
        _require_suffix_at(where, locator, page_text, span.suffix)


def require_page_locator(span: Span) -> PageCharRange:
    if not isinstance(span.locator, PageCharRange):
        raise LocatorKindUnreadable(
            f"a {span.locator.kind!r} span names no page of a file, so its quote cannot be "
            "read back")
    return span.locator


class SourceTextCache:
    """Page texts of the files one run read, each page extracted once."""

    def __init__(self) -> None:
        self._page_texts: dict[tuple[Path, str, int], str] = {}
        self._unchanged: set[tuple[Path, str]] = set()

    def read_page_text(self, path: Path, sha256: str, page: int) -> str:
        key = (path, sha256, page)
        if key not in self._page_texts:
            self._require_unchanged(path, sha256)
            self._page_texts[key] = read_page_text(path, page)
        return self._page_texts[key]

    def _require_unchanged(self, path: Path, sha256: str) -> None:
        if (path, sha256) in self._unchanged:
            return
        found = compute_sha256(path)
        if found != sha256:
            raise SourceChanged(
                f"{path.name} now hashes to {found}, not the {sha256} this run read it at")
        self._unchanged.add((path, sha256))


def _find_column_issues(
    rows: list[dict[str, Any]], column: Column, sources: Mapping[str, Path],
    texts: SourceTextCache,
) -> list[Issue]:
    refusals = [
        f"row {row}: {refusal}"
        for row, record in enumerate(rows)
        for cell in _list_span_cells(record[column.name], column.type, column.fields)
        if (refusal := _find_refusal(cell, sources, texts)) is not None
    ]
    if not refusals:
        return []
    shown = "; ".join(refusals[:_REFUSALS_SHOWN])
    more = "…" if len(refusals) > _REFUSALS_SHOWN else ""
    return [Issue(
        UserFacingErrorSeverity.error, column.name,
        f"{len(refusals)} span(s) do not hold in the file they quote: {shown}{more}",
    )]


def _find_refusal(cell: Any, sources: Mapping[str, Path], texts: SourceTextCache) -> str | None:
    try:
        verify_span(Span.model_validate(cell, strict=True), sources, texts)
    except ValidationError as err:
        return f"not a span: {'; '.join(format_errors(err))}"
    except SPAN_REFUSALS as refusal:
        return str(refusal)
    return None


def _holds_spans(column: Column) -> bool:
    element_type = column.type
    while (inner := find_list_element_type(element_type)) is not None:
        element_type = inner
    return element_type == SPAN_COLUMN_TYPE or any(
        _holds_spans(field) for field in column.fields or [])


def _list_span_cells(cell: Any, type_name: str, fields: list[Column] | None) -> Iterator[Any]:
    """Every span `cell` holds, through its list layers and a json object's declared fields."""
    if is_null_form(cell):
        return
    element_type = find_list_element_type(type_name)
    if element_type is not None:
        for element in cell:
            yield from _list_span_cells(element, element_type, fields)
    elif type_name == SPAN_COLUMN_TYPE:
        yield cell
    else:
        for field in fields or []:
            yield from _list_span_cells(cell.get(field.name), field.type, field.fields)


def _require_quote_at(where: str, locator: PageCharRange, page_text: str, quote: str) -> None:
    found = page_text[locator.start:locator.end]
    if normalize_text(found) != normalize_text(quote):
        raise QuoteNotAtAddress(
            f"quote not at {where}: characters {locator.start}–{locator.end} hold {found!r}, "
            f"not {quote!r}")


def _require_prefix_at(where: str, locator: PageCharRange, page_text: str, prefix: str) -> None:
    before = page_text[:locator.start]
    if not normalize_text(before).endswith(normalize_text(prefix)):
        raise QuoteNotAtAddress(
            f"prefix not at {where}: the text before character {locator.start} is "
            f"{before[-len(prefix):]!r}, not {prefix!r}")


def _require_suffix_at(where: str, locator: PageCharRange, page_text: str, suffix: str) -> None:
    after = page_text[locator.end:]
    if not normalize_text(after).startswith(normalize_text(suffix)):
        raise QuoteNotAtAddress(
            f"suffix not at {where}: the text after character {locator.end} is "
            f"{after[:len(suffix)]!r}, not {suffix!r}")
