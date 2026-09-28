from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import PageOutOfRange, UnsupportedTextFormat
from app.core.text_sources import count_pages, is_text_layer_empty, normalize_text, read_page_text
from pdf_fixture import write_text_pdf

FIRST_PAGE = "The first page says one thing."
SECOND_PAGE = "The second page says another."

# https://storage.courtlistener.com/recap/gov.uscourts.txnd.342881/gov.uscourts.txnd.342881.58.0.pdf
ECF_58_PAGE_1 = Path(__file__).parent / "fixtures" / "us_v_boeing_ecf58_page1.pdf"


@pytest.fixture
def two_pages(tmp_path: Path) -> Path:
    return write_text_pdf(tmp_path / "two_pages.pdf", [FIRST_PAGE, SECOND_PAGE])


def test_count_pages_counts_every_page_of_a_pdf(two_pages: Path) -> None:
    assert count_pages(two_pages) == 2


def test_read_page_text_reads_a_pdf_page_by_its_one_based_number(two_pages: Path) -> None:
    assert [read_page_text(two_pages, 1), read_page_text(two_pages, 2)] == [FIRST_PAGE, SECOND_PAGE]


@pytest.mark.parametrize("page", [0, 3])
def test_a_page_outside_the_pdf_is_refused(two_pages: Path, page: int) -> None:
    with pytest.raises(PageOutOfRange, match=f"page {page} of a 2-page two_pages.pdf"):
        read_page_text(two_pages, page)


def test_a_pdf_with_no_text_layer_reads_as_empty(tmp_path: Path) -> None:
    blank = write_text_pdf(tmp_path / "blank.pdf", [""])
    assert read_page_text(blank, 1) == ""
    assert is_text_layer_empty(blank)


def test_a_pdf_with_text_on_one_page_has_a_text_layer(tmp_path: Path) -> None:
    assert not is_text_layer_empty(write_text_pdf(tmp_path / "half.pdf", ["", SECOND_PAGE]))


@pytest.mark.parametrize("name", ["notes.txt", "notes.md"])
def test_a_txt_or_md_file_is_one_page_holding_the_whole_file(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("Line one\nLine two — naïve\n", encoding="utf-8")
    assert count_pages(path) == 1
    assert read_page_text(path, 1) == "Line one\nLine two — naïve\n"


def test_a_missing_one_page_file_has_no_page_count(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        count_pages(tmp_path / "missing.md")


def test_page_two_of_a_one_page_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("Line one\n", encoding="utf-8")
    with pytest.raises(PageOutOfRange, match="page 2 of a 1-page notes.txt"):
        read_page_text(path, 2)


def test_an_html_page_reads_as_its_text_without_tags_scripts_or_styles(tmp_path: Path) -> None:
    path = tmp_path / "filing.html"
    path.write_text(
        "<html><head><style>p { color: red; }</style><script>var x = 1;</script></head>"
        "<body><p>Fish &amp; <b>chips</b></p>\n<p>Second</p></body></html>",
        encoding="utf-8",
    )
    assert read_page_text(path, 1) == "Fish & chips\nSecond"


def test_an_empty_txt_file_has_no_text_layer(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text(" \n", encoding="utf-8")
    assert is_text_layer_empty(path)


def test_a_file_with_no_page_reader_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "table.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(UnsupportedTextFormat, match="table.csv: page text is read only from"):
        count_pages(path)


def test_normalize_text_folds_compatibility_forms_soft_hyphens_and_whitespace() -> None:
    assert normalize_text("  eﬃcient exam­ple\n\t pro-\nceeds  ") == "efficient example pro- ceeds"


def test_the_ecf_stamp_reads_off_a_real_docket_page() -> None:
    stamp = "Case 4:21-cr-00005-O Document 58 Filed 02/08/22 Page 1 of 26 PageID 536"
    assert stamp in normalize_text(read_page_text(ECF_58_PAGE_1, 1))
