"""A model answers a span column with a quote; the runtime finds it and mints the Span."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from app.core.errors import QuoteAmbiguous, QuoteNotInText
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

# The refusals a model can act on, so its reply is sent back with their wording.
QUOTE_REFUSALS = (QuoteNotInText, QuoteAmbiguous)


class QuotedSpanColumn(NamedTuple):
    name: str
    quoted_from: Column


def find_quoted_span_columns(stage: LLMTransformStage) -> list[QuotedSpanColumn]:
    reads = {column.name: column for entry in stage.signature.reads for column in entry.columns}
    return [
        QuotedSpanColumn(column.name, reads[column.quoted_from])
        for column in stage.signature.adds
        if column.quoted_from is not None
    ]


def show_quoted_text(row: Row, columns: Sequence[QuotedSpanColumn]) -> Row:
    """The row a prompt renders: each quoted-from cell shows its text, not the span's fields."""
    column_types = {column.quoted_from.name: column.quoted_from.type for column in columns}
    shown = {name: _render_quoted_text(row[name], type_name) for name, type_name in column_types.items()}
    return {**row, **shown}


def complete_spans(
    reply: JsonDict, row: Row, columns: Sequence[QuotedSpanColumn],
    sources: Mapping[str, InputBinding],
) -> JsonDict:
    """Raises one of QUOTE_REFUSALS, worded for the model, when a quote names no one place."""
    completed = {
        column.name: _complete_span(
            reply[column.name], row[column.quoted_from.name], column.quoted_from, sources)
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


def _complete_span(
    reply_cell: Any, parent_cell: Any, quoted_from: Column, sources: Mapping[str, InputBinding],
) -> JsonDict | None:
    if reply_cell is None:
        return None
    reply = SpanReply.model_validate(reply_cell)
    if is_null_form(parent_cell):
        raise QuoteNotInText(
            f"`{quoted_from.name}` holds no text on this row to quote from: {reply.quote!r}")
    if quoted_from.type == LIST_SPAN_COLUMN_TYPE:
        spans = [Span.model_validate(cell, strict=True) for cell in parent_cell]
        return _pick_listed_span(spans, reply, quoted_from.name).model_dump()
    parent = Span.model_validate(parent_cell, strict=True)
    return _narrow_to_reply(parent, reply, _require_file_name(parent, sources)).model_dump()


def _narrow_to_reply(parent: Span, reply: SpanReply, file_name: str) -> Span:
    """The minted quote is the parent's own characters, whatever spacing the reply used."""
    page = normalize_with_offsets(parent.quote)
    quote, prefix, suffix = (_normalize(text) for text in (reply.quote, reply.prefix, reply.suffix))
    found = find_occurrences(page.text, quote) if quote else []
    framed = [
        start for start in found
        if _is_framed_by(page.text, start, start + len(quote), prefix, suffix)
    ]
    _require_one_place(found, framed, f"{label_locator(parent.locator)} of {file_name}", reply.quote)
    start, end = framed[0], framed[0] + len(quote)
    return narrow_span(
        parent, parent.quote[page.raw_starts[start]:page.raw_ends[end - 1]],
        prefix=_slice_raw_prefix(parent.quote, page, start, prefix),
        suffix=_slice_raw_suffix(parent.quote, page, end, suffix),
    )


def _pick_listed_span(spans: list[Span], reply: SpanReply, column_name: str) -> Span:
    quote = _normalize(reply.quote)
    matches: list[Span] = []
    for span in spans:
        if _normalize(span.quote) == quote and span not in matches:
            matches.append(span)
    if not matches:
        raise QuoteNotInText(
            f"quote is none of the {len(spans)} quotes in `{column_name}`; copy one of them "
            f"whole: {reply.quote!r}")
    if len(matches) > 1:
        raise QuoteAmbiguous(
            f"quote matches {len(matches)} spans in `{column_name}` at different places: "
            f"{reply.quote!r}")
    return matches[0]


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


def _require_one_place(found: list[int], framed: list[int], where: str, quote: str) -> None:
    if not found:
        raise QuoteNotInText(f"quote not found on {where}: {quote!r}")
    if not framed:
        raise QuoteNotInText(
            f"quote appears {len(found)} time(s) on {where}, none with the prefix and suffix "
            f"given: {quote!r}")
    if len(framed) > 1:
        raise QuoteAmbiguous(
            f"quote appears {len(framed)} times on {where}; give a prefix or suffix that tells "
            f"them apart: {quote!r}")


def _slice_raw_prefix(raw: str, page: NormalizedText, start: int, prefix: str) -> str | None:
    if not prefix:
        return None
    prefix_end = len(page.text[:start].rstrip())
    return raw[page.raw_starts[prefix_end - len(prefix)]:page.raw_starts[start]]


def _slice_raw_suffix(raw: str, page: NormalizedText, end: int, suffix: str) -> str | None:
    if not suffix:
        return None
    suffix_start = len(page.text) - len(page.text[end:].lstrip())
    return raw[page.raw_ends[end - 1]:page.raw_ends[suffix_start + len(suffix) - 1]]
