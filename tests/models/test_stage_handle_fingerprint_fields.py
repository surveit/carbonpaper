"""Every stage handle config class in app/models/stage.py (Connector,
LLMConfig, PythonFunction, JoinConfig, AggregateConfig, QueueConfig,
PublishConfig) partitions its own fields into `FINGERPRINT_FIELDS` (changes
what the stage computes) and `INCIDENTAL_FIELDS` (does not) — declared
EXPLICITLY, never computed from `model_fields`. That's the point: adding a
new field to a handle forces a classification decision here, rather than the
field silently falling into (or out of) `Stage.compute_definition_fingerprint`.
"""
from __future__ import annotations

import pytest

from app.models.stage import (
    AggregateConfig,
    Connector,
    JoinConfig,
    LLMConfig,
    PublishConfig,
    PythonFunction,
    QueueConfig,
)

_HANDLE_CONFIG_CLASSES = [
    Connector, LLMConfig, PythonFunction, JoinConfig, AggregateConfig, QueueConfig, PublishConfig,
]


@pytest.mark.parametrize("handle_cls", _HANDLE_CONFIG_CLASSES, ids=lambda c: c.__name__)
def test_fingerprint_and_incidental_fields_are_disjoint(handle_cls):
    overlap = handle_cls.FINGERPRINT_FIELDS & handle_cls.INCIDENTAL_FIELDS
    assert not overlap, f"{handle_cls.__name__}: field(s) {overlap} in both sets"


@pytest.mark.parametrize("handle_cls", _HANDLE_CONFIG_CLASSES, ids=lambda c: c.__name__)
def test_fingerprint_and_incidental_fields_cover_every_model_field(handle_cls):
    classified = handle_cls.FINGERPRINT_FIELDS | handle_cls.INCIDENTAL_FIELDS
    declared = set(handle_cls.model_fields)
    assert classified == declared, (
        f"{handle_cls.__name__}: FINGERPRINT_FIELDS | INCIDENTAL_FIELDS classifies "
        f"{sorted(classified)}, but the model declares {sorted(declared)} — an "
        "unclassified field exists (or a classified name no longer exists)"
    )
