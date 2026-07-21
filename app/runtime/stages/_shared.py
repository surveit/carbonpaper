"""Helpers shared by more than one stage handler module."""

from __future__ import annotations

import re


def _translate_where(expr: str) -> str:
    """Translate our SQL-ish predicate dialect to pandas eval syntax.

    Wraps AND/OR operands in parens so bitwise &/| binds the right way,
    and lowercases boolean literals."""
    e = expr
    e = e.replace(" IS NOT NULL", ".notna()")
    e = e.replace(" IS NULL", ".isna()")
    e = re.sub(r"\btrue\b", "True", e, flags=re.IGNORECASE)
    e = re.sub(r"\bfalse\b", "False", e, flags=re.IGNORECASE)

    def _split_wrap(s: str, sep: str, joiner: str) -> str:
        parts = [p.strip() for p in re.split(rf"\s+{sep}\s+", s)]
        if len(parts) <= 1:
            return s
        return f" {joiner} ".join(f"({p})" for p in parts)

    e = _split_wrap(e, "OR", "|")
    e = _split_wrap(e, "AND", "&")
    return e
