"""The timestamp id run ids, version ids and workflow-test run ids are all minted from."""
from __future__ import annotations

from datetime import datetime

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


def mint_timestamp_id() -> str:
    return datetime.now().strftime(_ID_FORMAT)
