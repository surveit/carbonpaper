"""One GET for a connector, waiting out a 429 only for as long as its Retry-After says."""
from __future__ import annotations

import time
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.core.errors import SourceUnavailable

MAX_RATE_LIMIT_WAITS = 5
MAX_RETRY_AFTER_S = 60
_TOO_MANY_REQUESTS = 429


def open_url(url: str, *, user_agent: str, timeout_s: float) -> BinaryIO:
    waits = 0
    while True:
        try:
            response: BinaryIO = urlopen(
                Request(url, headers={"User-Agent": user_agent}), timeout=timeout_s)
            return response
        except HTTPError as refused:  # before URLError, which it subclasses
            if refused.code != _TOO_MANY_REQUESTS:
                raise SourceUnavailable(f"GET {url} failed: {refused}") from refused
            if waits == MAX_RATE_LIMIT_WAITS:
                raise SourceUnavailable(
                    f"GET {url} was still rate-limited after {waits} waits") from refused
            time.sleep(_read_retry_after(url, refused))
            waits += 1
        except (URLError, TimeoutError) as failure:
            raise SourceUnavailable(f"GET {url} failed: {failure}") from failure


def read_url(url: str, *, user_agent: str, timeout_s: float) -> bytes:
    with open_url(url, user_agent=user_agent, timeout_s=timeout_s) as response:
        return response.read()


def _read_retry_after(url: str, refused: HTTPError) -> int:
    header = refused.headers.get("Retry-After")
    if header is None or not header.strip().isdigit():
        raise SourceUnavailable(
            f"GET {url} was rate-limited (429) with no Retry-After in seconds: {header!r}"
        ) from refused
    seconds = int(header)
    if seconds > MAX_RETRY_AFTER_S:
        raise SourceUnavailable(
            f"GET {url} was rate-limited (429) for {seconds}s, over the {MAX_RETRY_AFTER_S}s "
            "this run waits") from refused
    return seconds
