"""A pack-style locator kind, registered only inside `registered_docket_page()`."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.models.locators import (
    LOCATOR_KINDS,
    LocatorKindSpec,
    PageCharRange,
    register_locator_kind,
)

DOCKET_PAGE_KIND = "test_docket_page"


class DocketPage(PageCharRange):
    kind: str = DOCKET_PAGE_KIND
    entry: int


def label_docket_page(locator: DocketPage) -> str:
    return f"ECF No. {locator.entry} at {locator.page}"


@contextmanager
def registered_docket_page() -> Iterator[None]:
    register_locator_kind(LocatorKindSpec(model=DocketPage, label=label_docket_page))
    try:
        yield
    finally:
        del LOCATOR_KINDS[DOCKET_PAGE_KIND]
