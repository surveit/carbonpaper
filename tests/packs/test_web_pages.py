"""web_pages against local servers, never against the open web."""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.connectors import AcquiredBytes, UnreachedSource
from app.packs.web import connector
from app.packs.web.connector import WebPagesParams, acquire_web_pages
from pdf_fixture import write_text_pdf

_SENTENCE = ("The county board voted on the budget after a long public hearing, "
             "and the minutes record each amendment that was offered. ")
_ARTICLE = "<html><body><article><h1>Budget vote</h1>" + "".join(
    f"<p>{_SENTENCE}Paragraph {number}.</p>" for number in range(8)) + "</article></body></html>"
_SHORT = "<html><body><article><h1>Hi</h1><p>Short note.</p></article></body></html>"
_PLAIN = (_SENTENCE * 4).encode("utf-8")
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50
_Item = AcquiredBytes | UnreachedSource


@dataclass
class _Site:
    base: str = ""
    other_base: str = ""
    pdf: bytes = b""
    agents: list[str] = field(default_factory=list)


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Site]:
    served = _Site(pdf=write_text_pdf(tmp_path / "doc.pdf", [_SENTENCE * 4]).read_bytes())
    monkeypatch.setattr(connector, "_TIMEOUT_S", 1)

    class Landing(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            _reply(self, 200, "text/html; charset=utf-8", _ARTICLE.encode("utf-8"))

        def log_message(self, format: str, *args: object) -> None:
            return

    class Main(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            served.agents.append(self.headers.get("User-Agent", ""))
            routes = {
                "/article.html": (200, "text/html; charset=utf-8", _ARTICLE.encode("utf-8")),
                "/doc.pdf": (200, "application/pdf", served.pdf),
                "/pdf-as-octet": (200, "application/octet-stream", served.pdf),
                "/plain.txt": (200, "text/plain", _PLAIN),
                "/short.html": (200, "text/html", _SHORT.encode("utf-8")),
                "/image.png": (200, "image/png", _PNG),
                "/missing": (404, "text/html", b"gone"),
            }
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"{served.other_base}/landing.html")
                self.end_headers()
            elif self.path == "/slow":
                time.sleep(2.5)
            else:
                _reply(self, *routes[self.path])

        def log_message(self, format: str, *args: object) -> None:
            return

    main, served.base = _serve(Main)
    other, served.other_base = _serve(Landing)
    yield served
    main.shutdown()
    other.shutdown()


def _reply(handler: BaseHTTPRequestHandler, status: int, content_type: str, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _run(urls: list[str], **overrides: object) -> list[_Item]:
    params = WebPagesParams.model_validate(
        {"urls": [{"url": url} for url in urls], **overrides})
    return list(acquire_web_pages(params))


def _find_closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _one(site: _Site, path: str) -> _Item:
    (item,) = _run([site.base + path])
    return item


def test_an_article_page_is_reached(site: _Site) -> None:
    item = _one(site, "/article.html")

    assert isinstance(item, AcquiredBytes)
    assert item.filename == "1.html"
    assert item.origin_url == site.base + "/article.html"
    assert item.metadata == {
        "url_key": None, "requested_url": site.base + "/article.html",
        "final_url": site.base + "/article.html", "content_type": "text/html; charset=utf-8",
        "http_status": 200, "fetch_status": "ok"}
    with item.open_bytes() as stream:
        assert stream.read() == _ARTICLE.encode("utf-8")


def test_a_pdf_is_reached(site: _Site) -> None:
    item = _one(site, "/doc.pdf")

    assert isinstance(item, AcquiredBytes)
    assert item.filename.endswith(".pdf")
    assert item.metadata["content_type"] == "application/pdf"
    assert item.metadata["fetch_status"] == "ok"


def test_a_pdf_served_as_octet_stream_is_still_a_pdf(site: _Site) -> None:
    item = _one(site, "/pdf-as-octet")

    assert isinstance(item, AcquiredBytes)
    assert item.filename.endswith(".pdf")
    assert item.metadata["content_type"] == "application/octet-stream"
    assert item.metadata["fetch_status"] == "ok"


def test_a_plain_text_page_is_reached(site: _Site) -> None:
    item = _one(site, "/plain.txt")

    assert isinstance(item, AcquiredBytes)
    assert item.filename.endswith(".txt")
    assert item.metadata["fetch_status"] == "ok"


def test_a_redirect_is_followed_and_both_urls_kept(site: _Site) -> None:
    item = _one(site, "/redirect")

    assert isinstance(item, AcquiredBytes)
    assert item.origin_url == site.base + "/redirect"
    assert item.metadata["requested_url"] == site.base + "/redirect"
    assert item.metadata["final_url"] == site.other_base + "/landing.html"
    assert item.metadata["http_status"] == 200
    assert item.filename.endswith(".html")


def test_a_missing_page_is_unreached_as_http_404(site: _Site) -> None:
    item = _one(site, "/missing")

    assert isinstance(item, UnreachedSource)
    assert item.requested_url == site.base + "/missing"
    assert item.metadata == {
        "url_key": None, "requested_url": site.base + "/missing", "final_url": None,
        "content_type": None, "http_status": 404, "fetch_status": "http_404"}


def test_a_slow_page_is_unreached_as_timeout(site: _Site) -> None:
    item = _one(site, "/slow")

    assert isinstance(item, UnreachedSource)
    assert item.metadata["fetch_status"] == "timeout"
    assert item.metadata["http_status"] is None


def test_a_refused_connection_is_unreachable(
    site: _Site, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows takes about 2 s to report a refused connection, longer than the fixture's 1 s.
    monkeypatch.setattr(connector, "_TIMEOUT_S", 10)
    (item,) = _run([f"http://127.0.0.1:{_find_closed_port()}/nothing"])

    assert isinstance(item, UnreachedSource)
    assert item.metadata["fetch_status"] == "unreachable"


def test_a_url_that_is_not_http_is_invalid_and_not_requested(site: _Site) -> None:
    items = _run(["ftp://example.test/file", "not a url", "file:///etc/passwd"])

    assert [item.metadata["fetch_status"] for item in items] == ["invalid_url"] * 3
    assert all(isinstance(item, UnreachedSource) for item in items)
    assert site.agents == []


def test_an_image_is_unreached_as_unsupported_type(site: _Site) -> None:
    item = _one(site, "/image.png")

    assert isinstance(item, UnreachedSource)
    assert item.metadata["fetch_status"] == "unsupported_type"
    assert item.metadata["content_type"] == "image/png"
    assert item.metadata["http_status"] == 200
    assert item.metadata["final_url"] == site.base + "/image.png"


def test_a_page_with_too_little_text_is_stored_as_unreadable(site: _Site) -> None:
    item = _one(site, "/short.html")

    assert isinstance(item, AcquiredBytes)
    assert item.metadata["fetch_status"] == "unreadable"
    assert item.filename.endswith(".html")


def test_a_page_with_more_text_than_allowed_is_stored_as_too_long(site: _Site) -> None:
    (item,) = _run([site.base + "/plain.txt"], max_text_chars=100)

    assert isinstance(item, AcquiredBytes)
    assert item.metadata["fetch_status"] == "too_long"


def test_body_over_the_file_ceiling_is_too_long(
    site: _Site, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARBON_PAPER_MAX_UPLOAD_BYTES", "100")

    item = _one(site, "/article.html")

    assert isinstance(item, UnreachedSource)
    assert item.metadata["fetch_status"] == "too_long"
    assert item.metadata["http_status"] == 200


def test_a_mixed_list_reaches_some_and_marks_the_rest(site: _Site) -> None:
    paths = ["/article.html", "/missing", "/doc.pdf", "/image.png", "/plain.txt"]

    items = _run([site.base + path for path in paths])

    assert [item.metadata["requested_url"] for item in items] == [site.base + p for p in paths]
    assert [item.metadata["fetch_status"] for item in items] == [
        "ok", "http_404", "ok", "unsupported_type", "ok"]
    assert [isinstance(item, AcquiredBytes) for item in items] == [True, False, True, False, True]


def test_url_table_reads_urls_and_keys_from_a_csv(site: _Site, tmp_path: Path) -> None:
    table = tmp_path / "links.csv"
    table.write_text(
        f"report_no,link\n007,{site.base}/article.html\nB-2,{site.base}/missing\n",
        encoding="utf-8")

    params = WebPagesParams(url_table=str(table), url_column="link", key_column="report_no")
    first, second = list(acquire_web_pages(params))

    assert isinstance(first, AcquiredBytes) and first.filename == "007.html"
    assert first.metadata["url_key"] == "007"
    assert isinstance(second, UnreachedSource) and second.metadata["url_key"] == "B-2"


def test_a_key_is_made_safe_for_a_filename(site: _Site) -> None:
    params = WebPagesParams(urls=[{"url": site.base + "/article.html", "key": "../a b/c"}])

    (item,) = list(acquire_web_pages(params))

    assert isinstance(item, AcquiredBytes)
    assert item.filename == "_a_b_c.html"
    assert item.metadata["url_key"] == "../a b/c"


def test_params_refuse_both_urls_and_url_table(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        WebPagesParams(urls=[{"url": "http://a.test/"}], url_table=str(tmp_path / "t.csv"),
                       url_column="link")


def test_params_refuse_neither_urls_nor_url_table() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        WebPagesParams()


def test_params_refuse_a_url_listed_twice() -> None:
    with pytest.raises(ValidationError, match="http://a.test/x"):
        WebPagesParams(urls=[{"url": "http://a.test/x"}, {"url": "http://a.test/x"}])


def test_params_require_url_column_with_url_table(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="url_column"):
        WebPagesParams(url_table=str(tmp_path / "t.csv"))


def test_a_url_table_without_the_named_column_is_refused(tmp_path: Path) -> None:
    table = tmp_path / "links.csv"
    table.write_text("a,b\n1,2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="link"):
        list(acquire_web_pages(WebPagesParams(url_table=str(table), url_column="link")))


def test_every_request_names_carbon_paper_as_user_agent(site: _Site) -> None:
    _run([site.base + "/article.html", site.base + "/missing"])

    assert len(site.agents) == 2
    assert all("carbonpaper" in agent for agent in site.agents)
