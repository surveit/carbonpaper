"""A project's methodology — the authored prose the workflow was compiled from.

Kept out of the Project identity record deliberately: the home page loads every
project's Project record to build its cards, and the prose is the largest thing
a project owns.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.records.methodology import Methodology


@dataclass(frozen=True)
class MethodologyHeadline:
    """Either half is None where the prose carries it; nothing stands in."""

    title: str | None
    standfirst: str | None


def read_methodology(project_id: str) -> str | None:
    """The project's prose, or None when it has none — never a stand-in."""
    record = Methodology.load_or_none(project_id)
    return record.text if record is not None else None


def read_methodology_headline(project_id: str) -> MethodologyHeadline:
    text = read_methodology(project_id)
    if text is None:
        return MethodologyHeadline(title=None, standfirst=None)
    return MethodologyHeadline(
        title=_read_first_heading(text), standfirst=_read_opening_paragraph(text)
    )


def _read_first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
        if line.strip():
            return None
    return None


def _read_opening_paragraph(text: str) -> str | None:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not lines and (not stripped or stripped.startswith("#")):
            continue
        if not lines and _opens_a_block_that_is_not_prose(stripped):
            return None
        if not stripped:
            break
        lines.append(stripped)
    return " ".join(lines) or None


def _opens_a_block_that_is_not_prose(line: str) -> bool:
    # A list, quote, table or fence joined into one line reads as a sentence it is not.
    head = line.split(" ", 1)[0]
    return line[:1] in ("-", "*", "+", ">", "|", "`") or (
        head[-1:] in (".", ")") and head[:-1].isdigit()
    )


def write_methodology(project_id: str, text: str) -> None:
    """Raises ValueError on empty text — absence is stored as no record."""
    if not text.strip():
        raise ValueError("The methodology document is empty.")
    stored = Methodology.load_or_none(project_id)
    born = {"created_at": stored.created_at} if stored is not None else {}
    Methodology(id=project_id, text=text, **born).save()


def exists(project_id: str) -> bool:
    return Methodology.exists(project_id)
