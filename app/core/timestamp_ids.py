"""The timestamp id run ids, version ids and workflow-test run ids are all minted from."""
from __future__ import annotations

from datetime import datetime, timedelta
from threading import RLock

from app.core.ids import ID

# Fixed width, so an ordinary string sort over ids is chronological — the property
# app.services.versioning's version list and app.services.project's newest-run lookup
# both read by. Ids already on disk carry the older second-resolution form
# (`20260810T213500`, 15 characters); that form sorts before any id minted later in
# the same second, so mixing the two keeps the same order they happened in.
#
# Microseconds rather than seconds because at second resolution two ids minted in the
# same second are the SAME id: versions silently overwrote each other, run directories
# merged, and every test needing two distinct ids had to sleep a whole second to buy
# one. Microseconds do not make a collision impossible, only far narrower than the work
# between any two mints.
_ID_FORMAT = "%Y%m%dT%H%M%S.%f"


def mint_timestamp_id() -> ID:
    return datetime.now().strftime(_ID_FORMAT)


_stamp_lock = RLock()
_last_stamp: datetime | None = None


def now_iso() -> str:
    # Strictly increasing WITHIN a process only — two processes can still tie in one OS tick.
    global _last_stamp
    with _stamp_lock:
        now = datetime.now()
        if _last_stamp is not None and now <= _last_stamp:
            now = _last_stamp + timedelta(microseconds=1)
        _last_stamp = now
    return now.isoformat(timespec="microseconds")


def read_iso_stamp(value: object) -> datetime | None:
    """Naive stays naive: what basis a stamp without an offset carries is not known here."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read_orderable_stamp(value: object) -> datetime | None:
    """Reads a naive stamp as local, which only a sort position may assume — never a duration."""
    moment = read_iso_stamp(value)
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.astimezone()
