from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from app.models.errors import StepRefused
from app.packs.docket.files import RecapFilesParams
from app.packs.docket.mirror import MIRROR_MANIFEST
from app.runtime.branches import BranchRecorder
from app.runtime.starlark_code import compile_starlark_function
from app.services import workflow_summary
from app.services.project import WorkflowFile, import_project

_DATA = Path(__file__).resolve().parents[1] / "app" / "seeds" / "data"
_BUNDLE = _DATA / "trial_verdicts.json"
_MIRROR = _DATA / "trial_verdicts"


def test_the_bundle_imports_with_no_issues() -> None:
    project_id = import_project(WorkflowFile.model_validate_json(_BUNDLE.read_text(encoding="utf-8")))
    assert workflow_summary.read_workflow_summary(project_id).issues == []


def test_every_file_the_inputs_name_is_in_the_mirror_as_recorded() -> None:
    named = [file for stage_id in ("input_transcripts", "input_records")
             for file in RecapFilesParams.model_validate(
                 _read_stage(stage_id)["connector"]["params"]).files]
    records = [json.loads(line) for line in
               (_MIRROR / MIRROR_MANIFEST).read_text(encoding="utf-8").splitlines() if line]
    by_url = {record["source_url"]: record for record in records}
    for file in named:
        record = by_url[f"https://storage.courtlistener.com/{file.path}"]
        stored = (_MIRROR / record["path"]).read_bytes()
        assert hashlib.sha256(stored).hexdigest() == record["sha256"]
    assert len(named) == len(records)


def test_each_trial_lists_its_files_in_part_order_from_one() -> None:
    params = RecapFilesParams.model_validate(_read_stage("input_transcripts")["connector"]["params"])
    parts: dict[str, list[int]] = {}
    for file in params.files:
        parts.setdefault(file.case_number, []).append(file.part)
    assert all(found == list(range(1, len(found) + 1)) for found in parts.values())
    assert len(parts) == 9


def _read_stage(stage_id: str) -> dict:
    bundle = json.loads(_BUNDLE.read_text(encoding="utf-8"))
    (stage,) = [stage for stage in bundle["stages"] if stage["id"] == stage_id]
    return stage


def _compile(stage_id: str) -> Callable[[dict], dict]:
    recorder = BranchRecorder()
    handle = compile_starlark_function(
        _read_stage(stage_id)["starlark"]["code"], "transform", "transform", recorder)
    assert handle is not None

    def run(row: dict) -> dict:
        recorder.open_row(0)
        try:
            return handle(row)
        finally:
            recorder.close_row()

    return run


def _span(source_id: str, page: int, start: int, quote: str) -> dict:
    return {"source_id": source_id, "source_sha256": "f" * 64,
            "locator": {"kind": "page_char_range", "page": page, "start": start,
                        "end": start + len(quote)},
            "quote": quote, "prefix": None, "suffix": None}


_PAGES = [_span("day1", 1, 0, "INDEX. VERDICT 9"), _span("day1", 2, 0, "Testimony."),
          _span("day2", 1, 0, "Instructions end. (Jury out at 2:14.) We have a verdict."),
          _span("day2", 2, 0, "Guilty.")]


def _trial(cut: dict | None, leaks: list[dict]) -> dict:
    return {"case_name": "United States v. Doe", "pages": _PAGES, "deliberations_begin": cut,
            "outcome_leaks": leaks}


def test_the_strip_keeps_pages_before_the_cut_and_the_cut_page_up_to_the_quote() -> None:
    cut = _span("day2", 1, 18, "(Jury out at 2:14.)")
    leak = _span("day1", 1, 7, "VERDICT 9")

    row = _compile("strip_verdict")(_trial(cut, [leak]))

    assert row["transcript"] == [
        _PAGES[1], _span("day2", 1, 0, "Instructions end. (Jury out at 2:14.)")]
    assert (row["pages_kept"], row["pages_removed"], row["leak_pages_removed"]) == (2, 1, 1)


def test_the_strip_refuses_a_transcript_with_no_point_where_deliberations_began() -> None:
    with pytest.raises(StepRefused, match=r"United States v\. Doe: no quote marks"):
        _compile("strip_verdict")(_trial(None, []))


def test_the_strip_refuses_a_leak_on_the_page_where_deliberations_began() -> None:
    cut = _span("day2", 1, 18, "(Jury out at 2:14.)")
    with pytest.raises(StepRefused, match="sits on the page where deliberations began, page 1"):
        _compile("strip_verdict")(_trial(cut, [_span("day2", 1, 0, "Instructions end.")]))


def _verdicts(*verdicts: tuple[int, str]) -> list[dict]:
    return [{"count": count, "charge": "c", "verdict": verdict, "finding": None}
            for count, verdict in verdicts]


@pytest.mark.parametrize("jury, model, agreement, lines", [
    ([(1, "guilty"), (2, "guilty")], [(1, "guilty"), (2, "guilty")], "agree", []),
    ([(1, "guilty"), (2, "guilty")], [(1, "guilty"), (2, "not_guilty")], "partial",
     ["Count 2: the jury found guilty; the model found not_guilty"]),
    ([(1, "guilty")], [(1, "not_guilty")], "disagree",
     ["Count 1: the jury found guilty; the model found not_guilty"]),
    ([(1, "guilty")], [(2, "guilty")], "no_counts_compared",
     ["Count 1: the jury found guilty; the model gave no verdict",
      "Count 2: the model found guilty; the jury's verdict names no such count"]),
    ([(1, "no_verdict")], [(1, "guilty")], "disagree",
     ["Count 1: the jury found no_verdict; the model found guilty"]),
])
def test_the_comparison_matches_counts_by_number(
    jury: list[tuple[int, str]], model: list[tuple[int, str]], agreement: str, lines: list[str],
) -> None:
    row = _compile("compare_verdicts")(
        {"jury_verdicts": _verdicts(*jury), "verdicts": _verdicts(*model)})
    assert (row["agreement"], row["disagreements"]) == (agreement, lines)


def test_the_comparison_names_a_trial_whose_judge_reply_failed() -> None:
    row = _compile("compare_verdicts")({"jury_verdicts": _verdicts((1, "guilty")), "verdicts": None})
    assert (row["agreement"], row["counts_compared"]) == ("no_model_verdict", 0)


def _record(*verdicts: tuple[int, str]) -> list[dict]:
    return [{"count": count, "verdict": verdict} for count, verdict in verdicts]


@pytest.mark.parametrize("read, record, agreement, lines", [
    ([(1, "guilty"), (2, "guilty")], _record((1, "guilty"), (2, "guilty")), "agree", []),
    ([(1, "guilty"), (2, "guilty")], _record((1, "guilty"), (2, "not_guilty")), "disagree",
     ["Count 2: the transcript reading says guilty; the judgment says not_guilty"]),
    ([(1, "no_verdict")], _record(), "disagree",
     ["Count 1: the transcript reading says no_verdict; the judgment names no such count"]),
    ([(1, "guilty")], _record((1, "guilty"), (3, "guilty")), "disagree",
     ["Count 3: the judgment says guilty; the transcript reading names no such count"]),
    ([(1, "guilty")], None, "no_record",
     ["No judgment for this trial is in the mirror, so the transcript reading goes unchecked"]),
])
def test_the_ground_truth_check_matches_the_judgment_by_count(
    read: list[tuple[int, str]], record: list[dict] | None, agreement: str, lines: list[str],
) -> None:
    row = _compile("check_ground_truth")(
        {"jury_verdicts": _verdicts(*read), "record_verdicts": record})
    assert (row["record_agreement"], row["record_notes"]) == (agreement, lines)
