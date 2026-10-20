from __future__ import annotations

from pathlib import Path

from app.core.files import compute_sha256
from app.core.text_sources import read_page_text
from app.models.locators import PageCharRange
from app.models.run_manifest import InputBinding
from app.models.spans import Span
from app.runtime.spans import SourceTextCache, find_span_refusal

_FIRST = "The county board voted on the budget after a long public hearing. " * 5
_SECOND = "Residents who spoke against the plan said the notice period was too short. " * 5


def _write_article(path: Path) -> None:
    path.write_text(
        "<html><head><title>News</title></head><body><nav>Home | About</nav>"
        f"<article><p>{_FIRST.strip()}</p><p>{_SECOND.strip()}</p></article>"
        "<footer>© Site</footer></body></html>",
        encoding="utf-8",
    )


def test_html_reader_returns_the_article_not_the_navigation(tmp_path: Path) -> None:
    path = tmp_path / "news.html"
    _write_article(path)
    text = read_page_text(path, 1)
    assert _FIRST.strip() in text
    assert _SECOND.strip() in text
    assert "Home | About" not in text
    assert "© Site" not in text


def test_html_reader_decodes_a_declared_windows_1252_page(tmp_path: Path) -> None:
    path = tmp_path / "news.html"
    body = f"{_FIRST} café – “quoted”. {_SECOND}"
    path.write_bytes(
        ('<html><head><meta charset="windows-1252"></head><body><article>'
         f"<p>{body}</p></article></body></html>").encode("cp1252"))
    assert "café – “quoted”" in read_page_text(path, 1)


def test_html_reader_returns_empty_for_a_page_with_no_main_text(tmp_path: Path) -> None:
    path = tmp_path / "empty.html"
    path.write_text("<html><body><nav>x</nav></body></html>", encoding="utf-8")
    assert read_page_text(path, 1) == ""


def test_html_reader_gives_identical_text_on_every_read(tmp_path: Path) -> None:
    path = tmp_path / "news.html"
    _write_article(path)
    assert read_page_text(path, 1) == read_page_text(path, 1)


def test_a_span_quoted_from_article_text_verifies(tmp_path: Path) -> None:
    path = tmp_path / "news.html"
    _write_article(path)
    text = read_page_text(path, 1)
    quote = "notice period was too short"
    start = text.index(quote)
    sha = compute_sha256(path)
    span = Span(
        source_id="news", source_sha256=sha, quote=quote,
        locator=PageCharRange(page=1, start=start, end=start + len(quote)))
    binding = InputBinding(
        stage_id="load", path=str(path), filename="news.html", sha256=sha, file_id="news")
    sources = {sha: binding}
    assert find_span_refusal(span, sources, SourceTextCache()) is None
