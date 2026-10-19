"""A PageCharRange subclass with its own field, kind and page check, registered only in a `with`."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.core.errors import QuoteNotAtAddress
from app.core.text_sources import normalize_text
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

    def validate_text(self, text: str) -> None:
        # A federal court stamps each filed page "Document <entry>" in its header.
        if f"Document {self.entry} " not in normalize_text(text):
            raise QuoteNotAtAddress(f"page {self.page} is not stamped as ECF No. {self.entry}")


def label_docket_page(locator: DocketPage) -> str:
    return f"ECF No. {locator.entry} at {locator.page}"


@contextmanager
def registered_docket_page() -> Iterator[None]:
    register_locator_kind(LocatorKindSpec(model=DocketPage, label=label_docket_page))
    try:
        yield
    finally:
        del LOCATOR_KINDS[DOCKET_PAGE_KIND]
