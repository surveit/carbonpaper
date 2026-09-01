"""The partition is declared EXPLICITLY, never computed from `model_fields`,
so that adding a field to a config block forces a classification decision here."""
from __future__ import annotations

import pytest
from typing import get_args

from app.models.stage import PythonFunction, Stage
from app.models.stages.stage_base import AbstractStage
from app.models.stages.aggregate import AggregateConfig
from app.models.stages.dedupe import DedupeConfig
from app.models.stages.explode import ExplodeConfig
from app.models.stages.human_review_queue import QueueConfig
from app.models.stages.input_data import Connector
from app.models.stages.join import JoinConfig
from app.models.stages.llm_transform import LLMConfig
from app.models.stages.filter_rows import FilterConfig
from app.models.stages.report import ReportConfig
from app.models.stages.sort_rank import SortRankConfig
from app.models.stages.starlark import StarlarkFunction
from app.models.stages.starlark_filter import StarlarkFilter
from app.models.stages.starlark_report import StarlarkReport
from app.models.stages.union import UnionConfig

# Every class a stage's `fingerprint_blocks()` can return, so a block that
# reaches the fingerprint cannot skip the classification below.
_CONFIG_CLASSES = [
    Connector, LLMConfig, PythonFunction, JoinConfig, AggregateConfig, QueueConfig,
    ReportConfig, UnionConfig, FilterConfig, StarlarkFunction,
    ExplodeConfig, DedupeConfig, SortRankConfig, StarlarkFilter, StarlarkReport,
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


def _config_block_fields(stage_cls) -> set[str]:
    own = set(stage_cls.model_fields) - set(AbstractStage.model_fields)
    return {
        name for name in own
        if isinstance(stage_cls.model_fields[name].annotation, type)
        and issubclass(stage_cls.model_fields[name].annotation, tuple(_CONFIG_CLASSES))
    }


@pytest.mark.parametrize("stage_cls", get_args(get_args(Stage)[0]), ids=lambda c: c.__name__)
def test_fingerprint_blocks_names_every_config_block_the_type_declares(stage_cls):
    """Pins the bug where a report fingerprinted one config block, so code edits kept the cache."""
    declared = _config_block_fields(stage_cls)
    # model_construct skips validation, so the blocks can be sentinels: this
    # asks which fields fingerprint_blocks() reads, not what is in them.
    named = set(stage_cls.model_construct(**{name: None for name in declared}).fingerprint_blocks())
    assert named == declared, (
        f"{stage_cls.__name__}: fingerprint_blocks() names {sorted(named)}, but the "
        f"model declares the config block(s) {sorted(declared)} — an unfingerprinted "
        "block means editing it does not invalidate a cached stage result"
    )
