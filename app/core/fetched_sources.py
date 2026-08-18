"""Downloading a source someone else publishes, to a path fixed by the URL.

Holding the first copy is what lets a re-run mean what the first run meant: a publisher
editing the file underneath cannot silently change what a finished run said.
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

from app.core.errors import SourceFetchError
from app.core.files import max_upload_bytes
from app.core.store_config import resolve_db_path

# Says who is calling and where to complain, the courtesy a public data host is owed.
USER_AGENT = "carbon-paper (+https://github.com/surveit/carbonpaper)"

_READ_TIMEOUT_SECONDS = 300
_CHUNK_BYTES = 1024 * 1024

# What bytes are stored under when the URL's own path ends in nothing usable — an
# `/api/grants` endpoint serving csv is the common case.
_FALLBACK_FILENAME = "fetched.dat"


def resolve_fetched_path(url: str, *, refetch: bool = False) -> Path:
    """The local path holding `url`'s bytes, downloading only when there is no copy yet."""
    destination = fetched_path_for(url)
    if destination.is_file() and not refetch:
        return destination
    _download_to(url, destination)
    return destination


def fetched_path_for(url: str) -> Path:
    """Fixed by the URL, so presence on disk IS whether it has been fetched."""
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    # Hashed rather than spelled into the path: a query string holds characters a path
    # cannot, and two URLs differing only past a truncation would collide.
    return fetched_sources_root() / key / _filename_for(url)


def fetched_sources_root() -> Path:
    """Beside the document store and the upload store, so pinning the DB path carries it."""
    return resolve_db_path().parent / "fetched"


def _download_to(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Staged beside the destination and moved onto it, so a torn read never becomes a
    # copy a later run would hold and trust.
    staged = destination.with_name(destination.name + ".part")
    try:
        with _open_stream(url) as response, staged.open("wb") as out:
            _copy_under_ceiling(response, out, url)
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)


def _copy_under_ceiling(response: BinaryIO, out: BinaryIO, url: str) -> None:
    """Counted while streaming: a publisher sending no Content-Length still cannot overrun."""
    ceiling = max_upload_bytes()
    written = 0
    while chunk := response.read(_CHUNK_BYTES):
        written += len(chunk)
        if written > ceiling:
            raise SourceFetchError(
                f"{url} is over the {ceiling} byte ceiling for one input. Raise "
                "CARBON_PAPER_MAX_UPLOAD_BYTES with the machine, or fetch a narrower slice."
            )
        out.write(chunk)


def _open_stream(url: str) -> BinaryIO:
    """urlopen's own errors name neither the URL nor what wanted it."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(request, timeout=_READ_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as refused:
        raise SourceFetchError(
            f"{url} answered {refused.code} {refused.reason}. A dataset behind a bot wall "
            "has to be fetched by hand and uploaded as a file input instead."
        ) from refused
    except urllib.error.URLError as unreachable:
        raise SourceFetchError(
            f"{url} could not be reached: {unreachable.reason}"
        ) from unreachable


def _filename_for(url: str) -> str:
    """The published name, so a run manifest shows the reader something recognisable."""
    name = Path(urlparse(url).path).name
    # The URL is foreign input and this is joined onto a directory.
    return name if name and "/" not in name and name not in (".", "..") else _FALLBACK_FILENAME
