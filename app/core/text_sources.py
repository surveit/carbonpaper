"""Page text of a file: a PDF page by page through pypdf; a txt, md or html file is one page."""
from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple

from pypdf import PdfReader

from app.core.errors import PageOutOfRange, UnsupportedTextFormat

_PDF_SUFFIX = ".pdf"
_SOFT_HYPHEN = "\u00ad"
_UNRENDERED_HTML_TAGS = frozenset({"script", "style"})


def count_pages(path: Path) -> int:
    if _is_pdf(path):
        return len(PdfReader(path).pages)
    _require_one_page_reader(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    return 1


def read_page_text(path: Path, page: int) -> str:
    """`page` is 1-based; a PDF page with no text layer reads as ''."""
    if _is_pdf(path):
        pages = PdfReader(path).pages
        _require_page_in_range(path, page, len(pages))
        return pages[page - 1].extract_text()
    read_one_page = _resolve_one_page_reader(path)
    _require_page_in_range(path, page, 1)
    return read_one_page(path)


def read_every_page_text(path: Path) -> Iterator[str]:
    """Page 1 first, from one open of the file."""
    if _is_pdf(path):
        return (page.extract_text() for page in PdfReader(path).pages)
    return iter([_resolve_one_page_reader(path)(path)])


def normalize_text(text: str) -> str:
    compatible = unicodedata.normalize("NFKC", text)
    unhyphenated = compatible.replace(_SOFT_HYPHEN, "")
    return " ".join(unhyphenated.split())


class NormalizedText(NamedTuple):
    text: str
    # Where in the raw text each character of `text` came from: raw[raw_starts[i]:raw_ends[i]].
    raw_starts: list[int]
    raw_ends: list[int]


def normalize_with_offsets(raw: str) -> NormalizedText:
    """normalize_text, one base character and its combining marks at a time, so offsets map back."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    space_pending = False
    for start, end in _split_combining_clusters(raw):
        for char in unicodedata.normalize("NFKC", raw[start:end]).replace(_SOFT_HYPHEN, ""):
            if char.isspace():
                space_pending = bool(chars)
                continue
            if space_pending:
                chars.append(" ")
                starts.append(start)
                ends.append(start)
                space_pending = False
            chars.append(char)
            starts.append(start)
            ends.append(end)
    return NormalizedText("".join(chars), starts, ends)


def is_text_layer_empty(path: Path) -> bool:
    return not any(text.strip() for text in read_every_page_text(path))


def is_text_source(path: Path) -> bool:
    return _is_pdf(path) or path.suffix.lower() in _ONE_PAGE_READERS


def _is_pdf(path: Path) -> bool:
    return path.suffix.lower() == _PDF_SUFFIX


def _split_combining_clusters(raw: str) -> Iterator[tuple[int, int]]:
    start = 0
    for index in range(1, len(raw)):
        if not unicodedata.combining(raw[index]):
            yield start, index
            start = index
    if raw:
        yield start, len(raw)


def _require_page_in_range(path: Path, page: int, page_count: int) -> None:
    if not 1 <= page <= page_count:
        raise PageOutOfRange(f"page {page} of a {page_count}-page {path.name}")


def _resolve_one_page_reader(path: Path) -> Callable[[Path], str]:
    _require_one_page_reader(path)
    return _ONE_PAGE_READERS[path.suffix.lower()]


def _require_one_page_reader(path: Path) -> None:
    if path.suffix.lower() not in _ONE_PAGE_READERS:
        readable = ", ".join(sorted([_PDF_SUFFIX, *_ONE_PAGE_READERS]))
        raise UnsupportedTextFormat(f"{path.name}: page text is read only from {readable}")


def _read_utf8_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_html_text(path: Path) -> str:
    parser = _HtmlTextParser()
    parser.feed(_read_utf8_text(path))
    parser.close()
    return "".join(parser.chunks)


class _HtmlTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._inside_unrendered_tag = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _UNRENDERED_HTML_TAGS:
            self._inside_unrendered_tag = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _UNRENDERED_HTML_TAGS:
            self._inside_unrendered_tag = False

    def handle_data(self, data: str) -> None:
        if not self._inside_unrendered_tag:
            self.chunks.append(data)


_ONE_PAGE_READERS: dict[str, Callable[[Path], str]] = {
    ".txt": _read_utf8_text,
    ".md": _read_utf8_text,
    ".html": _read_html_text,
}
