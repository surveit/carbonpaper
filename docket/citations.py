import re

from pydantic import BaseModel

_SECTION = re.compile(r"(?P<part>\d+)\.(?P<section>\d+(?:-\d+)?[A-Za-z]*)")
_PARAGRAPH = re.compile(r"\(([A-Za-z0-9]{1,4})\)")


class Citation(BaseModel):
    cfr_title: int
    cfr_part: str
    section: str
    paragraph: str | None = None

    def key(self) -> str:
        return f"{self.cfr_title}:{self.cfr_part}.{self.section}"

    def display(self) -> str:
        tail = self.paragraph or ""
        return f"{self.cfr_title} CFR {self.cfr_part}.{self.section}{tail}"


def parse_citation(raw: str, default_title: int) -> Citation | None:
    """Section numbers like 86.1869-12 carry a hyphenated suffix that must survive the parse."""
    cleaned = raw.replace(" ", " ").replace(" ", " ")
    title = _read_title(cleaned, default_title)
    match = _SECTION.search(re.sub(r"\b\d+\s+CFR\b", " ", cleaned, flags=re.IGNORECASE))
    if match is None:
        return None
    return Citation(
        cfr_title=title,
        cfr_part=match.group("part"),
        section=match.group("section"),
        paragraph=_read_paragraph(cleaned, match.end()),
    )


def _read_title(text: str, default_title: int) -> int:
    match = re.search(r"\b(\d{1,2})\s+CFR\b", text, re.IGNORECASE)
    return int(match.group(1)) if match else default_title


def _read_paragraph(text: str, offset: int) -> str | None:
    """Everything after the section number, so (aa) and (g)(4) stay distinguishable."""
    tail = text[offset:]
    stop = re.search(r"[^\s()A-Za-z0-9]", tail)
    tail = tail[: stop.start()] if stop else tail
    found = _PARAGRAPH.findall(tail)
    return "".join(f"({piece})" for piece in found) if found else None
