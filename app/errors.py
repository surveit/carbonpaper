"""errors.py — project exceptions, declared centrally and dependency-free so any
layer can catch them without import cycles."""

from __future__ import annotations


class RegenerateWithoutSnapshotError(Exception):
    """Raised when a from-scratch compile would overwrite reviewed work without a
    prior version snapshot and without explicit confirm_overwrite."""
