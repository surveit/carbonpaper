"""web_pages: each URL in a list, fetched; a readable one is a stored source, the rest say why not."""
from __future__ import annotations

import io
import re
import tempfile
from collections.abc import Hashable, Iterator, Mapping
from dataclasses import dataclass
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from typing import BinaryIO, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from pydantic import model_validator
from pypdf.errors import PyPdfError

from app.core.errors import SourceUnavailable
from app.core.files import max_upload_bytes
from app.core.source_files import read_source_file, resolve_file_format
from app.core.text_sources import read_every_page_text
from app.core.url_fetch import open_url
from app.models.base import _Base
from app.models.connectors import (
    AbsolutePath, AcquiredBytes, ConnectorParams, ConnectorSpec, MetadataValue, UnreachedSource,
)
from app.models.schema import Column

USER_AGENT = "carbonpaper (https://github.com/surveit/carbonpaper)"
_TIMEOUT_S = 30
_CHUNK_BYTES = 1 << 16
_PDF_MAGIC = b"%PDF-"
_SUFFIX_BY_CONTENT_TYPE = {"text/html": ".html", "text/plain": ".txt"}
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class UrlEntry(_Base):
    url: str
    key: str | None = None


class WebPagesParams(ConnectorParams):
    urls: list[UrlEntry] = []
    url_table: AbsolutePath | None = None
    url_column: str | None = None
    key_column: str | None = None
    # A fetched page with fewer text characters than this is stored as `unreadable`.
    min_text_chars: int = 200
    # A fetched page with more text characters than this is stored as `too_long`.
    max_text_chars: int = 2000000

    @model_validator(mode="after")
    def _require_one_list_of_urls(self) -> Self:
        if bool(self.urls) == (self.url_table is not None):
            raise ValueError("give exactly one of urls and url_table")
        if self.url_table is not None and self.url_column is None:
            raise ValueError("url_column is required with url_table")
        _refuse_repeated_urls(self.urls)
        return self


def acquire_web_pages(params: WebPagesParams) -> Iterator[AcquiredBytes | UnreachedSource]:
    entries = params.urls if params.urls else _read_url_table(params)
    for index, entry in enumerate(entries, start=1):
        yield _acquire_entry(index, entry, params)


@dataclass(frozen=True)
class _Fetched:
    final_url: str
    content_type: str | None
    http_status: int
    body: bytes


def _acquire_entry(
    index: int, entry: UrlEntry, params: WebPagesParams,
) -> AcquiredBytes | UnreachedSource:
    scheme = urlsplit(entry.url).scheme
    if scheme not in ("http", "https") or not urlsplit(entry.url).netloc:
        return _build_unreached(entry, "invalid_url")
    fetched = _fetch(entry)
    if isinstance(fetched, UnreachedSource):
        return fetched
    suffix = _find_suffix(fetched)
    if suffix is None:
        return _build_unreached(entry, "unsupported_type", fetched)
    status = _judge_text(fetched.body, suffix, params)
    return AcquiredBytes(
        filename=_name_file(index, entry, suffix), origin_url=entry.url,
        open_bytes=lambda: io.BytesIO(fetched.body),
        metadata=_build_metadata(entry, status, fetched))


def _fetch(entry: UrlEntry) -> _Fetched | UnreachedSource:
    head: _Fetched | None = None
    try:
        with open_url(entry.url, user_agent=USER_AGENT, timeout_s=_TIMEOUT_S) as response:
            head = _read_head(response)
            body = _read_capped(response, max_upload_bytes())
    except SourceUnavailable as unavailable:
        return _build_unreached_from_failure(entry, unavailable.__cause__)
    except (HTTPException, OSError) as failure:
        return _build_unreached_from_failure(entry, failure, head)
    if body is None:
        return _build_unreached(entry, "too_long", head)
    return _Fetched(head.final_url, head.content_type, head.http_status, body)


def _read_head(response: BinaryIO) -> _Fetched:
    if not isinstance(response, HTTPResponse):
        raise TypeError(f"open_url returned {type(response).__name__}, not an HTTP response")
    return _Fetched(
        response.geturl(), response.headers.get("Content-Type"), response.status, b"")


def _read_capped(response: BinaryIO, ceiling: int) -> bytes | None:
    """None when the body is longer than `ceiling` bytes."""
    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(_CHUNK_BYTES):
        total += len(chunk)
        if total > ceiling:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _build_unreached_from_failure(
    entry: UrlEntry, cause: BaseException | None, head: _Fetched | None = None,
) -> UnreachedSource:
    if isinstance(cause, HTTPError):
        return _build_unreached(entry, f"http_{cause.code}", http_status=cause.code)
    timed_out = isinstance(cause, TimeoutError) or (
        isinstance(cause, URLError) and isinstance(cause.reason, TimeoutError))
    return _build_unreached(entry, "timeout" if timed_out else "unreachable", head)


def _find_suffix(fetched: _Fetched) -> str | None:
    if fetched.body.startswith(_PDF_MAGIC):
        return ".pdf"
    media_type = (fetched.content_type or "").split(";")[0].strip().lower()
    return _SUFFIX_BY_CONTENT_TYPE.get(media_type)


def _judge_text(body: bytes, suffix: str, params: WebPagesParams) -> str:
    length = _measure_text_length(body, suffix)
    if length is None or length < params.min_text_chars:
        return "unreadable"
    return "too_long" if length > params.max_text_chars else "ok"


def _measure_text_length(body: bytes, suffix: str) -> int | None:
    """The characters read_pages will see; None when the reader cannot open the bytes."""
    with tempfile.TemporaryDirectory() as directory:
        with tempfile.NamedTemporaryFile(dir=directory, suffix=suffix, delete=False) as staged:
            staged.write(body)
        try:
            return len("".join(read_every_page_text(Path(staged.name))).strip())
        except (PyPdfError, ValueError):  # ValueError covers UnicodeDecodeError
            return None


def _name_file(index: int, entry: UrlEntry, suffix: str) -> str:
    stem = _UNSAFE_FILENAME_CHARS.sub("_", entry.key or "").lstrip(".")
    return f"{stem or index}{suffix}"


def _build_unreached(
    entry: UrlEntry, status: str, fetched: _Fetched | None = None, *, http_status: int | None = None,
) -> UnreachedSource:
    return UnreachedSource(
        requested_url=entry.url, metadata=_build_metadata(entry, status, fetched, http_status))


def _build_metadata(
    entry: UrlEntry, status: str, fetched: _Fetched | None = None, http_status: int | None = None,
) -> dict[str, MetadataValue]:
    return {
        "url_key": entry.key, "requested_url": entry.url,
        "final_url": fetched.final_url if fetched else None,
        "content_type": fetched.content_type if fetched else None,
        "http_status": fetched.http_status if fetched else http_status,
        "fetch_status": status}


def _refuse_repeated_urls(entries: list[UrlEntry]) -> None:
    urls = [entry.url for entry in entries]
    repeated = sorted({url for url in urls if urls.count(url) > 1})
    if repeated:
        raise ValueError(f"urls lists {repeated} more than once")


def _read_url_table(params: WebPagesParams) -> list[UrlEntry]:
    assert params.url_table is not None and params.url_column is not None
    path = Path(params.url_table)
    frame = read_source_file(path, resolve_file_format(params.url_table), dtype=str)
    wanted = [params.url_column, *([params.key_column] if params.key_column else [])]
    missing = [name for name in wanted if name not in frame.columns]
    if missing:
        raise ValueError(f"{path.name} has no column {missing}; it has {list(frame.columns)}")
    rows = frame.to_dict("records")
    entries = [_read_table_entry(number, row, params) for number, row in enumerate(rows, start=1)]
    _refuse_repeated_urls(entries)
    return entries


def _read_table_entry(number: int, row: Mapping[Hashable, object], params: WebPagesParams) -> UrlEntry:
    assert params.url_column is not None
    url = row[params.url_column]
    if not isinstance(url, str) or not url.strip():  # a blank cell reads as NaN, not str
        raise ValueError(f"{params.url_table} row {number} has no {params.url_column!r}")
    key = row[params.key_column] if params.key_column else None
    return UrlEntry(url=url.strip(), key=key if isinstance(key, str) else None)


_METADATA_COLUMNS = (
    Column(name="url_key", type="str", nullable=True),
    Column(name="requested_url", type="str", nullable=True),
    Column(name="final_url", type="str", nullable=True),
    Column(name="content_type", type="str", nullable=True),
    Column(name="http_status", type="int", nullable=True),
    Column(name="fetch_status", type="str", nullable=True),
)

WEB_PAGES = ConnectorSpec(
    kind="web_pages", params_model=WebPagesParams,
    metadata_columns=_METADATA_COLUMNS, acquire=acquire_web_pages)
