from __future__ import annotations

import json
import random
from collections.abc import Callable
from pathlib import Path

import pytest

from app.models.errors import StepRefused
from app.packs.opinions.connector import CourtListenerOpinionsParams
from app.runtime.branches import BranchRecorder
from app.runtime.starlark_code import compile_starlark_function
from app.services import workflow_summary
from app.services.project import WorkflowFile, import_project

_DATA = Path(__file__).resolve().parents[1] / "app" / "seeds" / "data"
_BUNDLE = _DATA / "qualified_immunity_appeals.json"
_SAMPLE_FRAME = _DATA / "qualified_immunity" / "sample_frame.json"
_SAMPLE_SEED = 20261014
_SAMPLE_PER_CIRCUIT = 10


def test_the_bundle_imports_with_no_issues() -> None:
    project_id = import_project(WorkflowFile.model_validate_json(_BUNDLE.read_text(encoding="utf-8")))
    assert workflow_summary.read_workflow_summary(project_id).issues == []


def test_the_input_stage_names_a_valid_opinion_search() -> None:
    stage = _read_input_stage()
    assert stage["connector"]["kind"] == "courtlistener_opinions"
    CourtListenerOpinionsParams.model_validate(stage["connector"]["params"])


def test_the_input_stage_reads_the_seeded_draw_from_the_committed_frame() -> None:
    frame = json.loads(_SAMPLE_FRAME.read_text(encoding="utf-8"))
    one_generator = random.Random(_SAMPLE_SEED)
    drawn = [
        cluster_id for court_id in ("ca5", "ca9")
        for cluster_id in one_generator.sample(
            sorted(row["cluster_id"] for row in frame
                   if row["court_id"] == court_id and row["stored_pdfs"] == 1),
            _SAMPLE_PER_CIRCUIT)]
    assert set(drawn) == set(_read_input_stage()["connector"]["params"]["cluster_ids"])


def _read_input_stage() -> dict:
    bundle = json.loads(_BUNDLE.read_text(encoding="utf-8"))
    (stage,) = [s for s in bundle["stages"] if s["type"] == "input_data"]
    return stage


def _compile_ruling_filter() -> Callable[[dict], object]:
    bundle = json.loads(_BUNDLE.read_text(encoding="utf-8"))
    (stage,) = [s for s in bundle["stages"] if s["id"] == "ruling_pages"]
    recorder = BranchRecorder()
    handle = compile_starlark_function(
        stage["starlark_filter"]["code"], "should_include", "should_include", recorder)
    assert handle is not None

    def decide(row: dict) -> object:
        recorder.open_row(0)
        try:
            return handle(row)
        finally:
            recorder.close_row()

    return decide


def _page(*, ruling: str | None, ruling_quote: dict | None, claim: str | None) -> dict:
    return {"case_name": "Doe v. Roe", "cluster_id": 777, "page": 4, "ruling": ruling,
            "ruling_quote": ruling_quote, "claim": claim}


def test_the_ruling_filter_keeps_a_page_with_a_full_holding_and_drops_one_with_none() -> None:
    keeps = _compile_ruling_filter()
    assert keeps(_page(ruling="granted", ruling_quote={"quote": "q"}, claim="Roe's tasing")) is True
    assert keeps(_page(ruling=None, ruling_quote=None, claim=None)) is False


@pytest.mark.parametrize("ruling, ruling_quote, claim", [
    ("granted", {"quote": "q"}, None),
    ("granted", None, "Roe's tasing"),
    (None, {"quote": "q"}, "Roe's tasing"),
    ("granted", None, None),
    (None, {"quote": "q"}, None),
    (None, None, "Roe's tasing"),
])
def test_the_ruling_filter_refuses_a_page_with_only_part_of_a_holding(
        ruling: str | None, ruling_quote: dict | None, claim: str | None) -> None:
    keeps = _compile_ruling_filter()
    with pytest.raises(StepRefused, match=r"Doe v\. Roe \(cluster 777\), page 4"):
        keeps(_page(ruling=ruling, ruling_quote=ruling_quote, claim=claim))
