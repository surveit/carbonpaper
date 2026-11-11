"""A model answers a span column with a quote; the runtime finds it and mints the Span."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from itertools import pairwise
from typing import Any, NamedTuple

from app.core.errors import QuoteAmbiguous, QuoteRefused
from app.core.frames import is_null_form
from app.core.json_types import JsonDict
from app.core.text_sources import NormalizedText, normalize_with_offsets
from app.models.locators import label_locator
from app.models.run_manifest import InputBinding
from app.models.schema import LIST_SPAN_COLUMN_TYPE, Column
from app.models.spans import Span, SpanReply, find_occurrences, narrow_span
from app.models.stages.llm_transform import LLMTransformStage

from ..spans import require_bound_file
from .execution import Row


class QuotedSpanColumn(NamedTuple):
    name: str
    quoted_from: Column
    type: str


def find_quoted_span_columns(stage: LLMTransformStage) -> list[QuotedSpanColumn]:
    reads = {column.name: column for entry in stage.signature.reads for column in entry.columns}
    return [
        QuotedSpanColumn(column.name, reads[column.quoted_from], column.type)
        for column in stage.signature.adds
        if column.quoted_from is not None
    ]


def render_quoted_text_in_row(row: Row, columns: Sequence[QuotedSpanColumn]) -> Row:
    """The row a prompt renders: each quoted-from cell shows its text, not the span's fields."""
    column_types = {column.quoted_from.name: column.quoted_from.type for column in columns}
    shown = {name: _render_quoted_text(row[name], type_name) for name, type_name in column_types.items()}
    return {**row, **shown}


def complete_spans(
    reply: JsonDict, row: Row, columns: Sequence[QuotedSpanColumn],
    sources: Mapping[str, InputBinding],
) -> JsonDict:
    """Raises QuoteRefused, which a re-ask can fix, or QuoteAmbiguous, which it cannot."""
    completed = {
        column.name: _complete_cell(
            reply[column.name], row[column.quoted_from.name], column, sources)
        for column in columns
    }
    return {**reply, **completed}


def _render_quoted_text(cell: Any, column_type: str) -> Any:
    if is_null_form(cell):
        return cell
    if column_type == LIST_SPAN_COLUMN_TYPE:
        # JSON, so each quote reads as one string however many lines it runs to.
        return json.dumps([span["quote"] for span in cell], ensure_ascii=False, indent=2)
    return cell["quote"]


class _NormalizedSpan(NamedTuple):
    span: Span
    page: NormalizedText


class _Place(NamedTuple):
    parent: _NormalizedSpan
    start: int


def _complete_cell(
    reply_cell: Any, parent_cell: Any, column: QuotedSpanColumn,
    sources: Mapping[str, InputBinding],
) -> JsonDict | list[JsonDict] | None:
    if reply_cell is None:
        return None
    if column.type == LIST_SPAN_COLUMN_TYPE:
        minted = _mint_replies(reply_cell, parent_cell, column.quoted_from, sources)
        return [span.model_dump() for span in minted]
    (span,) = _mint_replies([reply_cell], parent_cell, column.quoted_from, sources)
    return span.model_dump()


def _mint_replies(
    reply_cells: Sequence[Any], parent_cell: Any, quoted_from: Column,
    sources: Mapping[str, InputBinding],
) -> list[Span]:
    replies = [SpanReply.model_validate(cell) for cell in reply_cells]
    if not replies:
        return []
    if is_null_form(parent_cell):
        empty = f"`{quoted_from.name}` holds no text on this row to quote from: {replies[0].quote!r}"
        raise QuoteRefused(empty, correction=empty)
    find_quote = _prepare_quote_finder(parent_cell, quoted_from, sources)
    return [find_quote(reply) for reply in replies]


def _prepare_quote_finder(
    parent_cell: Any, quoted_from: Column, sources: Mapping[str, InputBinding],
) -> Callable[[SpanReply], Span]:
    """Normalizes the quoted-from text once, for every quote in the reply."""
    if quoted_from.type == LIST_SPAN_COLUMN_TYPE:
        listed = _drop_repeats([Span.model_validate(cell, strict=True) for cell in parent_cell])
        return partial(_match_listed_span, _normalize_spans(listed), quoted_from.name)
    parent = Span.model_validate(parent_cell, strict=True)
    where = f"on {label_locator(parent.locator)} of {_require_file_name(parent, sources)}"
    return partial(_narrow_to_one_place, _normalize_spans([parent]), where)


def _match_listed_span(listed: list[_NormalizedSpan], column_name: str, reply: SpanReply) -> Span:
    quote = _normalize(reply.quote)
    whole = [one.span for one in listed if one.page.text == quote]
    if len(whole) > 1:
        # A listed span is picked by its quote alone, so no re-ask can tell these apart.
        raise QuoteAmbiguous(
            f"the {len(whole)} quotes in `{column_name}` reading {reply.quote!r} are "
            f"indistinguishable: they sit at different places, and a quote cannot pick one")
    if whole:
        return whole[0]
    return _narrow_to_one_place(listed, f"in the {len(listed)} spans of `{column_name}`", reply)


def _narrow_to_one_place(
    parents: Sequence[_NormalizedSpan], where: str, reply: SpanReply,
) -> Span:
    """The minted quote is the parent's own characters, whatever spacing the reply used."""
    quote, prefix, suffix = (_normalize(text) for text in (reply.quote, reply.prefix, reply.suffix))
    found = _find_places(parents, quote)
    if not found and _is_split_across_neighbours(parents, quote):
        raise _refuse_quote(
            "quote runs across two consecutive strings", where, reply.quote,
            "A quote must come from one string of the list; this one runs from the end of one "
            "into the start of the next. Quote only the words within one string.")
    framed = [
        place for place in found
        if _is_framed_by(place.parent.page.text, place.start, place.start + len(quote),
                         prefix, suffix)
    ]
    _require_one_place(len(found), len(framed), where, reply.quote)
    return _mint_at(framed[0], quote, prefix, suffix)


def _find_places(parents: Sequence[_NormalizedSpan], quote: str) -> list[_Place]:
    if not quote:
        return []
    return [
        _Place(parent, start)
        for parent in parents
        for start in find_occurrences(parent.page.text, quote)
    ]


def _is_split_across_neighbours(parents: Sequence[_NormalizedSpan], quote: str) -> bool:
    # Splits only at a space of the normalized quote, so neither part cuts a word in two.
    splits = [(quote[:at], quote[at + 1:]) for at in find_occurrences(quote, " ")]
    return any(
        head in first.page.text and tail in second.page.text
        for first, second in pairwise(parents)
        for head, tail in splits
    )


def _mint_at(place: _Place, quote: str, prefix: str, suffix: str) -> Span:
    parent, page = place.parent
    start, end = place.start, place.start + len(quote)
    return narrow_span(
        parent, parent.quote[page.raw_starts[start]:page.raw_ends[end - 1]],
        prefix=_take_raw_prefix(parent.quote, page, start, prefix),
        suffix=_take_raw_suffix(parent.quote, page, end, suffix),
    )


def _drop_repeats(spans: list[Span]) -> list[Span]:
    return list({span.model_dump_json(): span for span in spans}.values())


def _normalize_spans(spans: list[Span]) -> list[_NormalizedSpan]:
    return [_NormalizedSpan(span, normalize_with_offsets(span.quote)) for span in spans]


def _require_file_name(parent: Span, sources: Mapping[str, InputBinding]) -> str:
    # A stage test binds no files, so there the file goes by its stored id.
    if parent.source_sha256 not in sources:
        return f"stored file {parent.source_id}"
    return require_bound_file(parent.source_id, parent.source_sha256, sources).filename


def _normalize(text: str | None) -> str:
    return normalize_with_offsets(text or "").text


def _is_framed_by(text: str, start: int, end: int, prefix: str, suffix: str) -> bool:
    # `text` is normalized, so at most one space sits between the quote and its context.
    return text[:start].rstrip().endswith(prefix) and text[end:].lstrip().startswith(suffix)


def _require_one_place(found: int, framed: int, where: str, quote: str) -> None:
    if not found:
        raise _refuse_quote("quote not found", where, quote,
                            "Copy the words exactly as they appear in the text you were shown.")
    if not framed:
        raise _refuse_quote(
            f"the prefix and suffix given frame none of the {found} places the quote "
            "appears", where, quote,
            "Copy the prefix and suffix exactly as they appear in the text you were shown.")
    if framed > 1:
        raise _refuse_quote(
            f"quote appears {framed} times", where, quote,
            "Give a prefix or suffix, copied exactly, that tells them apart, or quote more words.")


def _refuse_quote(finding: str, where: str, quote: str, fix: str) -> QuoteRefused:
    # The model was never shown a file name or page, so its correction names neither.
    return QuoteRefused(f"{finding} {where}: {quote!r}", correction=f"{finding}: {quote!r}. {fix}")


def _take_raw_prefix(raw: str, page: NormalizedText, start: int, prefix: str) -> str | None:
    if not prefix:
        return None
    prefix_end = len(page.text[:start].rstrip())
    return raw[page.raw_starts[prefix_end - len(prefix)]:page.raw_starts[start]]


def _take_raw_suffix(raw: str, page: NormalizedText, end: int, suffix: str) -> str | None:
    if not suffix:
        return None
    suffix_start = len(page.text) - len(page.text[end:].lstrip())
    return raw[page.raw_ends[end - 1]:page.raw_ends[suffix_start + len(suffix) - 1]]
