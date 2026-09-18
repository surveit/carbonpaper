"""What a captured eval case is refused for: an unreadable case, or a run that did not replay."""
from __future__ import annotations


class CaseInvalid(Exception):
    pass


class CaseDidNotReplay(Exception):
    pass
