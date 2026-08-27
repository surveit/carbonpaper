"""Retiring a publish stage's `template`, whose markup now lives in `function.code`.

Shared by alembic revision 0012 (the document store) and
the alembic revision that rewrites the stored payloads, so a rewritten
store and a rewritten compiled file cannot disagree about what a spec meant.
"""
from __future__ import annotations

from typing import Any

_MISSING = object()

# What the field said is kept, not deleted: every stored `template` in the
# examples holds authored prose (a column list, an output description), not the
# markup the field was named for, and a stage that emits a document is the only
# record of what it was meant to emit.
NOTE_PREFIX = "was publish.template: "


class PublishTemplateUnreadable(ValueError):
    """A `publish` payload shaped like nothing ReportConfig ever wrote."""


def move_publish_template_to_notes(spec: dict[str, Any]) -> bool:
    """Move one stage spec's `publish.template` into `compiler_notes`; False if unchanged."""
    # Idempotent: a spec with no `template` returns False untouched.
    publish = spec.get("publish")
    if publish is None:
        return False
    if not isinstance(publish, dict):
        raise PublishTemplateUnreadable(
            f"{spec.get('id', '?')}: `publish` is {type(publish).__name__}, not an object"
        )
    template = publish.pop("template", _MISSING)
    if template is _MISSING:
        return False
    if template:
        _append_note(spec, f"{NOTE_PREFIX}{template}")
    return True


def _append_note(spec: dict[str, Any], note: str) -> None:
    notes = spec.setdefault("compiler_notes", [])
    if not isinstance(notes, list):
        raise PublishTemplateUnreadable(
            f"{spec.get('id', '?')}: `compiler_notes` is {type(notes).__name__}, not a list"
        )
    if note not in notes:
        notes.append(note)
