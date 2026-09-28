from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.errors import (
    LocatorKindUnreadable,
    PageOutOfRange,
    QuoteNotAtAddress,
    SourceChanged,
    SourceIdMismatch,
    SourceNotRead,
)
from app.core.files import compute_sha256
from app.core.frames import table_from_rows
from app.core.text_sources import read_page_text
from app.models import Column, TableSchema
from app.models.locators import CellAt, Locator, PageCharRange
from app.models.run_manifest import InputBinding
from app.models.spans import Span, narrow_span
from app.runtime import spans as spans_module
from app.runtime.spans import SourceTextCache, find_span_issues, verify_span
from app.runtime.validation import Issue
from locator_kind_fixture import DocketPage, registered_docket_page
from pdf_fixture import write_text_pdf

STORED_FILE_ID = "stored_file"
FIRST_PAGE = "The first page says one thing."
SECOND_PAGE = "The second page says another."

# https://storage.courtlistener.com/recap/gov.uscourts.txnd.342881/gov.uscourts.txnd.342881.58.0.pdf
ECF_58_PAGE_1 = Path(__file__).parent / "fixtures" / "us_v_boeing_ecf58_page1.pdf"


@pytest.fixture
def two_pages(tmp_path: Path) -> Path:
    return write_text_pdf(tmp_path / "two_pages.pdf", [FIRST_PAGE, SECOND_PAGE])


def _whole_page(path: Path, page: int, locator: Locator | None = None) -> Span:
    text = read_page_text(path, page)
    return Span(
        source_id=STORED_FILE_ID, source_sha256=compute_sha256(path),
        locator=locator or PageCharRange(page=page, start=0, end=len(text)), quote=text,
    )


def _sources(*paths: Path, file_id: str | None = STORED_FILE_ID) -> dict[str, InputBinding]:
    """Each path as a run's input stage records it, stored under `file_id`."""
    return {
        (sha256 := compute_sha256(path)): InputBinding(
            stage_id="load", path=str(path), filename=path.name, sha256=sha256, file_id=file_id)
        for path in paths
    }


def _verify(span: Span, *paths: Path) -> None:
    verify_span(span, _sources(*paths), SourceTextCache())


def _moved(span: Span, **locator_fields: int) -> Span:
    """The same quote claimed at other coordinates, as a stage that miscounted would write it."""
    return span.model_copy(update={"locator": span.locator.model_copy(update=locator_fields)})


# ── verify_span ──────────────────────────────────────────────────────────────
def test_a_whole_page_span_verifies(two_pages: Path) -> None:
    _verify(_whole_page(two_pages, 2), two_pages)


def test_a_quote_narrowed_from_its_page_verifies(two_pages: Path) -> None:
    _verify(narrow_span(_whole_page(two_pages, 2), "says another"), two_pages)


def test_a_quote_not_at_its_address_is_refused_naming_file_page_and_both_texts(
    two_pages: Path,
) -> None:
    span = _moved(narrow_span(_whole_page(two_pages, 2), "second"), start=0, end=6)
    with pytest.raises(QuoteNotAtAddress) as refused:
        _verify(span, two_pages)
    assert str(refused.value) == (
        "quote not at page 2 of two_pages.pdf: characters 0–6 hold 'The se', not 'second'")


def test_a_quote_claimed_on_the_wrong_page_is_refused(two_pages: Path) -> None:
    span = _moved(narrow_span(_whole_page(two_pages, 2), "page says"), page=1)
    with pytest.raises(QuoteNotAtAddress, match="page 1 of two_pages.pdf"):
        _verify(span, two_pages)


def test_a_page_the_file_does_not_have_is_refused(two_pages: Path) -> None:
    with pytest.raises(PageOutOfRange, match="page 3 of a 2-page two_pages.pdf"):
        _verify(_moved(_whole_page(two_pages, 2), page=3), two_pages)


def test_a_span_naming_a_file_the_run_never_read_is_refused(
    two_pages: Path, tmp_path: Path
) -> None:
    other = write_text_pdf(tmp_path / "other.pdf", [FIRST_PAGE])
    with pytest.raises(SourceNotRead, match="this run read no file with sha256"):
        _verify(_whole_page(two_pages, 1), other)


def test_a_span_naming_another_stored_file_for_the_same_bytes_is_refused(two_pages: Path) -> None:
    span = _whole_page(two_pages, 1).model_copy(update={"source_id": "some_other_file"})
    with pytest.raises(SourceIdMismatch) as refused:
        _verify(span, two_pages)
    assert str(refused.value) == (
        "the span names stored file 'some_other_file', but this run read two_pages.pdf as "
        "stored file 'stored_file'")


def test_a_span_on_a_file_read_from_outside_the_store_is_refused(two_pages: Path) -> None:
    with pytest.raises(SourceIdMismatch, match="read two_pages.pdf from outside the file store"):
        verify_span(_whole_page(two_pages, 1), _sources(two_pages, file_id=None),
                    SourceTextCache())


def test_a_file_whose_bytes_changed_since_the_run_read_it_is_refused(two_pages: Path) -> None:
    span = _whole_page(two_pages, 1)
    sources = _sources(two_pages)
    write_text_pdf(two_pages, [SECOND_PAGE, FIRST_PAGE])
    with pytest.raises(SourceChanged, match="two_pages.pdf now hashes to"):
        verify_span(span, sources, SourceTextCache())


def test_a_span_with_no_page_to_read_is_refused(tmp_path: Path) -> None:
    table = tmp_path / "table.csv"
    table.write_text("memo\npaid in full\n", encoding="utf-8")
    span = Span(source_id=STORED_FILE_ID, source_sha256=compute_sha256(table),
                locator=CellAt(row=0, column="memo"), quote="paid in full")
    with pytest.raises(LocatorKindUnreadable, match="a 'cell' span names no page"):
        _verify(span, table)


def test_whitespace_the_page_breaks_differently_still_verifies(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("The first page\nsays one thing.", encoding="utf-8")
    span = Span(source_id=STORED_FILE_ID, source_sha256=compute_sha256(notes),
                locator=PageCharRange(page=1, start=4, end=19), quote="first page says")
    _verify(span, notes)


def test_a_prefix_and_suffix_that_surround_the_quote_verify(two_pages: Path) -> None:
    span = narrow_span(_whole_page(two_pages, 1), "page", prefix="first ", suffix=" says")
    _verify(span, two_pages)


@pytest.mark.parametrize(("context", "refusal"), [
    ({"prefix": "second "},
     "prefix not at page 1 of two_pages.pdf: the text before character 10 is ' first ', "
     "not 'second '"),
    ({"suffix": " said"},
     "suffix not at page 1 of two_pages.pdf: the text after character 14 is ' says', "
     "not ' said'"),
])
def test_a_prefix_or_suffix_that_does_not_surround_the_quote_is_refused(
    two_pages: Path, context: dict[str, str], refusal: str
) -> None:
    span = narrow_span(_whole_page(two_pages, 1), "page").model_copy(update=context)
    with pytest.raises(QuoteNotAtAddress) as refused:
        _verify(span, two_pages)
    assert str(refused.value) == refusal


def test_a_real_docket_page_verifies_a_quote_read_off_it() -> None:
    page = _whole_page(ECF_58_PAGE_1, 1)
    _verify(narrow_span(page, "IN VIOLATION OF THE CRIME VICTIMS’ RIGHTS ACT"), ECF_58_PAGE_1)


def test_a_kind_s_own_page_check_refuses_a_page_stamped_for_another_entry() -> None:
    text = read_page_text(ECF_58_PAGE_1, 1)
    with registered_docket_page():
        locator = DocketPage(entry=58, page=1, start=0, end=len(text))
        stamped = _whole_page(ECF_58_PAGE_1, 1, locator)
        _verify(stamped, ECF_58_PAGE_1)
        misfiled = _moved(stamped, entry=59)
        with pytest.raises(QuoteNotAtAddress, match="not stamped as ECF No. 59"):
            _verify(misfiled, ECF_58_PAGE_1)


# ── SourceTextCache ──────────────────────────────────────────────────────────
def test_two_spans_on_one_page_read_the_page_and_hash_the_file_once(
    two_pages: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[int] = []
    hashes: list[Path] = []

    def read_and_count(path: Path, page: int) -> str:
        reads.append(page)
        return read_page_text(path, page)

    def hash_and_count(path: Path) -> str:
        hashes.append(path)
        return compute_sha256(path)

    monkeypatch.setattr(spans_module, "read_page_text", read_and_count)
    monkeypatch.setattr(spans_module, "compute_sha256", hash_and_count)
    page = _whole_page(two_pages, 2)
    sources, texts = _sources(two_pages), SourceTextCache()
    for quote in ("second", "another"):
        verify_span(narrow_span(page, quote), sources, texts)
    assert (reads, hashes) == ([2], [two_pages])


# ── find_span_issues ─────────────────────────────────────────────────────────
def _find_issues(rows: list[dict[str, Any]], columns: list[Column], *paths: Path) -> list[Issue]:
    return find_span_issues(
        table_from_rows(rows), TableSchema(columns=columns), _sources(*paths), SourceTextCache())


def _column(name: str, type_name: str, **shape: Any) -> Column:
    return Column(name=name, type=type_name, nullable=True, **shape)


def test_a_table_whose_spans_all_verify_has_no_issues(two_pages: Path) -> None:
    rows: list[dict[str, Any]] = [{"basis": _whole_page(two_pages, 1).model_dump()}, {"basis": None}]
    assert _find_issues(rows, [_column("basis", "span")], two_pages) == []


def test_every_refused_span_in_a_column_is_counted_in_one_error_naming_its_row(
    two_pages: Path,
) -> None:
    good = _whole_page(two_pages, 1)
    rows = [{"basis": good.model_dump()},
            {"basis": _moved(good, page=2).model_dump()},
            {"basis": _moved(good, page=3).model_dump()}]
    (issue,) = _find_issues(rows, [_column("basis", "span")], two_pages)
    assert (issue.severity, issue.column) == ("error", "basis")
    assert issue.message.startswith(
        "2 span(s) do not hold in the file they quote: row 1: quote not at page 2")
    assert "; row 2: page 3 of a 2-page two_pages.pdf" in issue.message


def test_spans_in_a_list_and_inside_json_fields_are_each_verified(two_pages: Path) -> None:
    good = _whole_page(two_pages, 2).model_dump()
    bad = _moved(_whole_page(two_pages, 2), page=1).model_dump()
    columns = [
        _column("supporting", "list[span]"),
        _column("event", "json", fields=[_column("span", "span")]),
        _column("disputes", "list[json]", fields=[_column("contrary", "list[span]")]),
    ]
    rows = [{"supporting": [good, bad], "event": {"span": good},
             "disputes": [{"contrary": [good]}, {"contrary": [bad]}]},
            {"supporting": [good], "event": {"span": bad}, "disputes": []}]
    issues = _find_issues(rows, columns, two_pages)
    assert [(issue.column, issue.message.split(": ")[1]) for issue in issues] == [
        ("supporting", "row 0"), ("event", "row 1"), ("disputes", "row 0")]


def test_a_cell_naming_an_unregistered_locator_kind_is_refused(two_pages: Path) -> None:
    dump = _whole_page(two_pages, 1).model_dump()
    dump["locator"] = {**dump["locator"], "kind": "ecf_page"}
    (issue,) = _find_issues([{"basis": dump}], [_column("basis", "span")], two_pages)
    assert "row 0: not a span:" in issue.message
    assert "no locator kind 'ecf_page' is registered" in issue.message


def test_a_table_with_no_span_column_reads_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spans_module, "read_page_text", pytest.fail)
    assert _find_issues([{"memo": "paid"}], [_column("memo", "str")]) == []
