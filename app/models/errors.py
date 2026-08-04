"""Exceptions that are part of the authoring contract — the vocabulary authored
stage code is told to write, named by the models that describe it and raised by
the code the runtime executes."""
from __future__ import annotations


class StepRefused(Exception):
    """Seeded into inline authored code's namespace, so a step raises it without an import."""
