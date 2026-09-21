"""Exceptions raised by the developer scripts."""
from __future__ import annotations


class RunCaptureRefused(ValueError):
    """Nothing is captured unless the run finished and every file it read is still there."""
