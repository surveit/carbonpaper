from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from app.core.errors import QuoteAmbiguous, QuoteNotInText
from app.models.locators import CellAt, Locator, PageCharRange
from app.models.schema import Column, TableSchema
from app.models.spans import Span, SpanReply, narrow_span
from locator_kind_fixture import DocketPage, registered_docket_page

# "the plea" sits here twice; a double space and a newline separate the sentences.
_PAGE = "The court rejected the plea.  The court set a trial date.\nThe clerk read the plea aloud."


def _whole_page(locator: Locator | None = None, text: str = _PAGE) -> Span:
    return Span(
        source_id="stored_file",
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        locator=locator or PageCharRange(page=14, start=0, end=len(text)),
        quote=text,
    )


def _read_address(span: Span) -> str:
    assert isinstance(span.locator, PageCharRange)
    return _PAGE[span.locator.start:span.locator.end]


# ── narrow_span ──────────────────────────────────────────────────────────────
def test_a_quote_is_addressed_where_it_sits_on_the_page() -> None:
    page = _whole_page()
    quote = narrow_span(page, "rejected")
    assert quote.locator == PageCharRange(page=14, start=10, end=18)
    assert (quote.source_id, quote.source_sha256, quote.quote) == (
        page.source_id, page.source_sha256, "rejected")


def test_narrowing_a_narrowed_span_offsets_from_its_start() -> None:
    sentence = narrow_span(_whole_page(), "The court set a trial date.")
    date = narrow_span(sentence, "trial date")
    assert isinstance(sentence.locator, PageCharRange)
    assert isinstance(date.locator, PageCharRange)
    assert date.locator.start == sentence.locator.start + sentence.quote.index("trial date")
    assert _read_address(date) == "trial date"


def test_a_quote_not_on_the_page_is_refused() -> None:
    with pytest.raises(QuoteNotInText, match="quote not found in page 14: 'accepted the plea'"):
        narrow_span(_whole_page(), "accepted the plea")


@pytest.mark.parametrize("quote", ["plea. The court", "date. The clerk"])
def test_whitespace_is_never_normalised(quote: str) -> None:
    with pytest.raises(QuoteNotInText):
        narrow_span(_whole_page(), quote)


@pytest.mark.parametrize("prefix", [None, " "])
def test_a_quote_found_twice_is_ambiguous_unless_its_context_tells_them_apart(
    prefix: str | None,
) -> None:
    with pytest.raises(QuoteAmbiguous, match="quote appears 2 times in page 14"):
        narrow_span(_whole_page(), "the plea", prefix=prefix)


def test_a_quote_that_overlaps_itself_counts_every_place_it_starts() -> None:
    with pytest.raises(QuoteAmbiguous, match="2 times"):
        narrow_span(_whole_page(text="no, no, no."), "no, no")


def test_a_prefix_or_suffix_picks_one_of_two_occurrences() -> None:
    first = narrow_span(_whole_page(), "the plea", prefix="rejected ")
    second = narrow_span(_whole_page(), "the plea", suffix=" aloud")
    assert _read_address(first) == _read_address(second) == "the plea"
    assert isinstance(first.locator, PageCharRange)
    assert isinstance(second.locator, PageCharRange)
    assert _PAGE[:first.locator.start].endswith("rejected ")
    assert _PAGE[second.locator.end:].startswith(" aloud")
    assert (first.prefix, first.suffix, second.prefix, second.suffix) == (
        "rejected ", None, None, " aloud")


def test_context_that_matches_no_occurrence_is_not_in_the_text() -> None:
    with pytest.raises(QuoteNotInText, match="appears 2 time.* none with the prefix and suffix"):
        narrow_span(_whole_page(), "the plea", prefix="accepted ")


def test_an_empty_quote_is_refused() -> None:
    with pytest.raises(QuoteNotInText, match="empty quote"):
        narrow_span(_whole_page(), "")


def test_a_cell_span_holds_no_offsets_to_narrow() -> None:
    csv_text = "memo\npaid in full\n"
    cell = Span(source_id="stored_csv", source_sha256=hashlib.sha256(csv_text.encode()).hexdigest(),
                locator=CellAt(row=0, column="memo"), quote="paid in full")
    with pytest.raises(ValueError, match="'cell' span holds no character offsets"):
        narrow_span(cell, "paid")


def test_a_range_as_long_as_its_quote_is_required() -> None:
    with pytest.raises(ValidationError, match="covers 8 characters, but the quote has 17"):
        Span(source_id="stored_file", source_sha256=_whole_page().source_sha256,
             locator=PageCharRange(page=14, start=10, end=18), quote="rejected the plea")


def test_a_reply_quote_cannot_be_empty() -> None:
    with pytest.raises(ValidationError, match="quote"):
        SpanReply(quote="")


def test_a_subclass_keeps_its_added_fields_through_narrowing_and_a_dump() -> None:
    with registered_docket_page():
        page = _whole_page(DocketPage(page=14, start=0, end=len(_PAGE), entry=58))
        quote = narrow_span(page, "rejected")
        assert quote.locator == DocketPage(page=14, start=10, end=18, entry=58)
        assert Span.model_validate(quote.model_dump(), strict=True) == quote


# ── the span column type ─────────────────────────────────────────────────────
def test_quoted_from_is_refused_off_a_span_column() -> None:
    with pytest.raises(ValidationError, match="quoted_from is only valid on type 'span'"):
        Column(name="basis", type="str", nullable=True, quoted_from="page")


def test_a_frame_row_holds_a_whole_span() -> None:
    row_model = TableSchema(columns=[Column(name="basis", type="span", nullable=False)]
                            ).to_pydantic_model("row")
    span = narrow_span(_whole_page(), "rejected")
    row = row_model.model_validate({"basis": span.model_dump()}, strict=True)
    assert getattr(row, "basis") == span
    with pytest.raises(ValidationError):
        row_model.model_validate({"basis": "rejected"}, strict=True)


def test_a_reply_holds_words_and_no_address() -> None:
    reply_model = TableSchema(columns=[Column(name="basis", type="span", nullable=False)]
                              ).to_reply_model("reply")
    reply = reply_model.model_validate({"basis": {"quote": "the plea", "suffix": " aloud"}})
    assert getattr(reply, "basis") == SpanReply(quote="the plea", suffix=" aloud")
    with pytest.raises(ValidationError, match="source_id"):
        reply_model.model_validate({"basis": narrow_span(_whole_page(), "rejected").model_dump()})


def test_a_reader_of_a_span_column_need_not_repeat_quoted_from() -> None:
    produced = TableSchema(columns=[
        Column(name="basis", type="span", nullable=True, quoted_from="page")])
    read = TableSchema(columns=[Column(name="basis", type="span", nullable=True)])
    assert read.find_unsatisfied_columns(produced) == []
