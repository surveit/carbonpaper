from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.models.run_manifest import StageRecord
from app.services.review_packet.views import build_run_view
from app.web.run_issues import build_run_issues


def _build_record_missing_its_stage_id() -> dict[str, Any]:
    return {
        "type": "python_row_function", "status": "error",
        "input_validation_report": [], "output_validation_report": None,
        "output_row_count": 0,
    }


def test_a_stage_record_will_not_parse_without_its_stage_id() -> None:
    with pytest.raises(ValidationError) as refused:
        StageRecord.model_validate(_build_record_missing_its_stage_id())
    assert [error["loc"] for error in refused.value.errors()] == [("stage_id",)]


def test_the_run_issue_index_raises_on_a_record_missing_its_stage_id() -> None:
    manifest = {"stage_records": [_build_record_missing_its_stage_id()]}
    with pytest.raises(KeyError, match="stage_id"):
        build_run_issues(manifest, None)


def test_the_review_packet_view_raises_on_a_record_missing_its_stage_id() -> None:
    manifest = {"stage_records": [_build_record_missing_its_stage_id()]}
    with pytest.raises(KeyError, match="stage_id"):
        build_run_view(manifest, None)
