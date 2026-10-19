"""One page of a stored text Source, its text split around the characters a span's link names."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.core.text_sources import count_pages, read_page_text
from app.models.locators import CharRange


class SourcePage(BaseModel):
    number: int
    page_count: int
    character_count: int
    # The page's text in three runs, so the template marks the middle without building HTML.
    before: str
    marked: str
    after: str
    # The range the link named, where the page ends before it; nothing is marked then.
    range_off_page: CharRange | None


def build_source_page(path: Path, page: int, marked: CharRange | None) -> SourcePage:
    """Raises PageOutOfRange for a page the file does not have."""
    text = read_page_text(path, page)
    on_page = marked is not None and marked.end <= len(text)
    start, end = (marked.start, marked.end) if marked is not None and on_page else (0, 0)
    return SourcePage(
        number=page, page_count=count_pages(path), character_count=len(text),
        before=text[:start], marked=text[start:end], after=text[end:],
        range_off_page=None if on_page else marked,
    )
