"""Downloading a source someone else publishes, into the one file store.
The first copy is held, so a re-run reads the bytes the first run read: a publisher
editing the file underneath cannot silently change what a finished run said.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

from app.core.errors import (
    FileOverCeiling,
    SourceFetchError,
    StoreOverQuota,
)
from app.core.files import UploadedFile, resolve_stored_path, save_upload

# Says who is calling and where to complain, the courtesy a public data host is owed.
USER_AGENT = "carbon-paper (+https://github.com/surveit/carbonpaper)"

_READ_TIMEOUT_SECONDS = 300

# What bytes are stored under when the URL's own path ends in nothing usable — an
# `/api/grants` endpoint serving csv is the common case.
_FALLBACK_FILENAME = "fetched.dat"


def resolve_fetched_path(url: str, *, refetch: bool = False) -> Path:
    """The held copy's path; `refetch` takes today's bytes as a second record beside it."""
    held = None if refetch else find_fetched_file(url)
    return resolve_stored_path(held if held is not None else _fetch_into_store(url))


def find_fetched_file(url: str) -> UploadedFile | None:
    """The newest READABLE copy of `url`; None means nothing holds it and it must be fetched."""
    fetched = [record for record in UploadedFile.list() if record.source_url == url]
    # A full scan, as list_project_files is and for the same reason: the store selects by
    # id prefix only and a file's id is opaque. Fine at a workspace's worth of files.
    for record in sorted(fetched, key=lambda record: record.created_at, reverse=True):
        # A record whose bytes are gone is not a held copy — fetching again is the only
        # honest answer, and returning its path would name a file that is not there.
        if resolve_stored_path(record).is_file():
            return record
    return None


def _fetch_into_store(url: str) -> UploadedFile:
    with _open_stream(url) as response:
        return _save_under_store_limits(url, response)


def _save_under_store_limits(url: str, response: BinaryIO) -> UploadedFile:
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
