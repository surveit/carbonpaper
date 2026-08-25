"""Every calendar day between two dates, so a chart draws a quiet stretch as a gap."""
from __future__ import annotations

from datetime import date, timedelta


def days_spanned(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]
