"""Downloads a published source into the file store; the first copy is held for every re-run."""
from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

from app.core.errors import (
    FileOverCeiling,
    SourceFetchError,
    StoreOverQuota,
)
from app.core.files import ProjectFile, resolve_stored_path, save_upload

# Says who is calling and where to complain, the courtesy a public data host is owed.
USER_AGENT = "carbon-paper (+https://github.com/surveit/carbonpaper)"

_READ_TIMEOUT_SECONDS = 300

# The stored name when the URL path ends in no usable filename, e.g. `/api/grants`.
_FALLBACK_FILENAME = "fetched.dat"


def resolve_fetched_path(url: str, *, refetch: bool = False,
                         headers: Mapping[str, str] | None = None) -> Path:
    """The held copy's path; `refetch` takes today's bytes as a second record beside it."""
    held = None if refetch else find_fetched_file(url)
    if held is not None:
        return resolve_stored_path(held)
    return resolve_stored_path(_fetch_into_store(url, headers or {}))


def find_fetched_file(url: str) -> ProjectFile | None:
    """The newest READABLE copy of `url`; None means nothing holds it and it must be fetched."""
    fetched = ProjectFile.find(source_url=url)
    for record in sorted(fetched, key=lambda record: record.created_at, reverse=True):
        # A record whose bytes are gone is not a held copy: its path names a missing file.
        if resolve_stored_path(record).is_file():
            return record
    return None


def _fetch_into_store(url: str, headers: Mapping[str, str]) -> ProjectFile:
    with _open_stream(url, headers) as response:
        return _save_under_store_limits(url, response)


def _save_under_store_limits(url: str, response: BinaryIO) -> ProjectFile:
    """The ceiling and quota are the store's; only a fetch can say which URL ran into them."""
    try:
        return save_upload(_filename_for(url), response, source_url=url)
    except FileOverCeiling as too_big:
        raise SourceFetchError(
            f"{url} is over the {too_big.ceiling} byte ceiling for one input. Raise "
            "CARBON_PAPER_MAX_UPLOAD_BYTES with the machine, or fetch a narrower slice."
        ) from too_big
    except StoreOverQuota as no_room:
        raise SourceFetchError(
            f"{url} would take the file store to {no_room.used} bytes, past its "
            f"{no_room.quota}-byte limit. Delete files this workspace no longer needs."
        ) from no_room


def _open_stream(url: str, headers: Mapping[str, str]) -> BinaryIO:
    """urlopen's own errors name neither the URL nor what wanted it."""
    sent = {"User-Agent": USER_AGENT, **headers}   # last wins: a stage may name its own agent
    request = urllib.request.Request(url, headers=sent)
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
