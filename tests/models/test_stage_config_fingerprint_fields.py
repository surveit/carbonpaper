"""The partition is declared EXPLICITLY, never computed from `model_fields`,
so that adding a field to a config block forces a classification decision here."""
from __future__ import annotations

import pytest

from app.models.stage import PythonFunction
from app.models.stages.aggregate import AggregateConfig
from app.models.stages.human_review_queue import QueueConfig
from app.models.stages.input_data import Connector
from app.models.stages.join import JoinConfig
from app.models.stages.llm_transform import LLMConfig
from app.models.stages.filter_rows import FilterConfig
from app.models.stages.publish import PublishConfig
from app.models.stages.union import UnionConfig

# Every class a stage's `fingerprint_blocks()` can return, so a block that
# reaches the fingerprint cannot skip the classification below.
_CONFIG_CLASSES = [
    Connector, LLMConfig, PythonFunction, JoinConfig, AggregateConfig, QueueConfig,
    PublishConfig, UnionConfig, FilterConfig,
]


@pytest.mark.parametrize("config_cls", _CONFIG_CLASSES, ids=lambda c: c.__name__)
def test_fingerprint_and_incidental_fields_are_disjoint(config_cls):
    overlap = config_cls.FINGERPRINT_FIELDS & config_cls.INCIDENTAL_FIELDS
    assert not overlap, f"{config_cls.__name__}: field(s) {overlap} in both sets"


@pytest.mark.parametrize("config_cls", _CONFIG_CLASSES, ids=lambda c: c.__name__)
def test_fingerprint_and_incidental_fields_cover_every_model_field(config_cls):
    classified = config_cls.FINGERPRINT_FIELDS | config_cls.INCIDENTAL_FIELDS
    declared = set(config_cls.model_fields)
    assert classified == declared, (
        f"{config_cls.__name__}: FINGERPRINT_FIELDS | INCIDENTAL_FIELDS classifies "
        f"{sorted(classified)}, but the model declares {sorted(declared)} — an "
        "unclassified field exists (or a classified name no longer exists)"
    )
