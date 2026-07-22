import pytest
from pydantic import ValidationError
from app.models.stage import AggregateConfig


def test_having_is_now_rejected():
    with pytest.raises(ValidationError):
        AggregateConfig.model_validate({
            "group_by": ["x"],
            "aggregations": [{"output_column": "n", "formula": "count"}],
            "having": "n > 1",
        })


def test_aggregate_without_having_still_valid():
    cfg = AggregateConfig.model_validate({
        "group_by": ["x"],
        "aggregations": [{"output_column": "n", "formula": "count"}],
    })
    assert cfg.group_by == ["x"]
