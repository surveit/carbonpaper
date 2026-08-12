from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.models import Stage, StageType
from app.models.errors import StepRefused
from app.models.review_guide import ReviewGuideDraft
from app.runtime.context import RunContext
from app.runtime.stages import HANDLERS
from app.services import project, versioning
from app.services.loader import load_workflow
from app.services.project import WorkflowFile, import_project

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app" / "seeds" / "data" / "tutorial_lobbying_triage.json"
)
_CSV_PATH = _FIXTURE_PATH.with_suffix(".csv")
_COMMITMENTS_PATH = _FIXTURE_PATH.parent / "tutorial_public_commitments.csv"
_CSV_BY_STAGE_ID = {"raw_filings": _CSV_PATH, "public_commitments": _COMMITMENTS_PATH}
_GUIDE_PATH = _FIXTURE_PATH.parent / "review_guides" / _FIXTURE_PATH.name
# Long enough to say what to check, short enough that the check is what is read.
_GUIDE_PROSE_CEILING = 210

_EXPECTED_STAGE_IDS = [
    "raw_filings",
    "public_commitments",
    "matched_commitments",
    "judge_alignment",
    "flag_contradiction",
    "publish_report",
]

# Counted off the committed CSVs.
_ROWS_IN_CSV = 24
_COMMITMENT_ROWS = 15
# Filings whose client has no row in the commitments file.
_UNMATCHED = 8
_BATCH_SIZE = 12
# The cap the tour's first run passes as limits {"raw_filings": N}.
_TOUR_LIMIT = 6

_CONTRADICTS = "Contradicts"
_ALIGNMENT_VALUES = ["Contradicts", "Matches", "Unclear", "No commitment given"]


def _load_fixture() -> WorkflowFile:
    return WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _stage(wf: WorkflowFile, stage_id: str) -> Stage:
    return next(stage for stage in wf.stages if stage.id == stage_id)


def _execute(stage: Stage, inputs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    # The same ephemeral, run-dir-less context authored stage tests execute under.
    ctx = RunContext.for_stages_outside_a_run(None, None)
    result = HANDLERS[StageType(stage.type)].execute(stage, inputs, ctx)
    assert result is not None
    return result


def _all_filings() -> pd.DataFrame:
    return pd.read_csv(_CSV_PATH)


def _joined() -> pd.DataFrame:
    return _execute(
        _stage(_load_fixture(), "matched_commitments"),
        {
            "raw_filings": _all_filings(),
            "public_commitments": pd.read_csv(_COMMITMENTS_PATH),
        },
    )


def _stand_in_for_the_model(df: pd.DataFrame) -> pd.DataFrame:
    # Fills what judge_alignment would add. Never calls a model.
    judged = df.copy()
    judged["alignment"] = [
        "No commitment given" if pd.isna(said) else _ALIGNMENT_VALUES[i % 3]
        for i, said in enumerate(judged["public_commitment"])
    ]
    judged["alignment_note"] = [
        f"stand-in note for {filing_id}" for filing_id in judged["filing_id"]
    ]
    return judged


def test_committed_tutorial_fixture_imports_and_validates_cleanly(tmp_path):
    wf = _load_fixture()
    assert [stage.id for stage in wf.stages] == _EXPECTED_STAGE_IDS

    imported_name = import_project(wf, name="tutorial_smoke")
    assert imported_name in project.list_projects()

    summary = project.describe_workflow(imported_name)
    assert summary["issues"] == []
    assert [stage["id"] for stage in summary["stages"]] == _EXPECTED_STAGE_IDS

    project_dir = tmp_path / "examples" / imported_name
    loaded_stages = load_workflow(project_dir)
    assert [stage.id for stage in loaded_stages] == _EXPECTED_STAGE_IDS
    assert len(versioning.list_versions(project_dir)) == 1

    # The tutorial skips the data-model step, so the fixture carries no schemas.
    assert wf.data_model.schemas == []

    assert _CSV_PATH.is_file() and _COMMITMENTS_PATH.is_file()
    assert not (project_dir / "input").exists()


def test_the_bundled_filings_csv_carries_the_row_count_the_tour_reports():
    df = pd.read_csv(_CSV_PATH)

    assert list(df.columns) == [
        "filing_id", "client", "registrant", "amount_usd", "filing_period", "specific_issues",
    ]
    assert len(df) == _ROWS_IN_CSV


def test_the_bundled_commitments_csv_is_one_row_per_organisation():
    """What lets the join be an enrich: a repeated client would fail the run."""
    df = pd.read_csv(_COMMITMENTS_PATH)

    assert list(df.columns) == ["client", "public_commitment", "commitment_source"]
    assert len(df) == _COMMITMENT_ROWS
    assert df["client"].is_unique


def test_the_sample_shows_all_three_join_outcomes():
    """The tour needs a contradiction, an alignment and a non-match to point at."""
    joined = _joined()
    unmatched = joined[joined["public_commitment"].isna()]

    assert len(joined) == _ROWS_IN_CSV
    assert len(unmatched) == _UNMATCHED
    # Both sides of the say-versus-do frame are represented.
    asks = joined[joined["public_commitment"].notna()]["specific_issues"]
    assert int(asks.str.startswith("Opposing").sum()) > 0
    assert int(asks.str.startswith("Supporting").sum()) > 0


def test_the_texts_are_short_enough_to_read_side_by_side():
    """The point of this sample: the contradiction is visible in one glance."""
    filings = pd.read_csv(_CSV_PATH)
    commitments = pd.read_csv(_COMMITMENTS_PATH)

    assert int(filings["specific_issues"].str.len().max()) <= 120
    assert int(commitments["public_commitment"].str.len().max()) <= 120


def test_the_tours_first_six_filings_cover_a_contradiction_an_alignment_and_a_non_match():
    """Beat 2 caps raw_filings at 6 rows, so all three outcomes must be in there."""
    wf = _load_fixture()
    first_six = pd.read_csv(_CSV_PATH).head(_TOUR_LIMIT)

    joined = _execute(
        _stage(wf, "matched_commitments"),
        {"raw_filings": first_six, "public_commitments": pd.read_csv(_COMMITMENTS_PATH)},
    )

    assert len(joined) == _TOUR_LIMIT
    assert int(joined["public_commitment"].isna().sum()) == 2
    matched = joined[joined["public_commitment"].notna()]["specific_issues"]
    assert int(matched.str.startswith("Opposing").sum()) >= 1
    assert int(matched.str.startswith("Supporting").sum()) >= 1


# ── the join ─────────────────────────────────────────────────────────────────


def test_the_join_is_many_to_one_and_drops_no_filing():
    joined = _joined()
    filings = _all_filings()

    assert len(joined) == len(filings) == _ROWS_IN_CSV
    assert list(joined["filing_id"]) == list(filings["filing_id"])
    # Every subject column flows through untouched; the join only ever ADDS.
    for column in filings.columns:
        assert list(joined[column]) == list(filings[column])
    assert list(joined.columns)[-2:] == ["public_commitment", "commitment_source"]


def test_one_commitment_serves_several_filings_by_the_same_client():
    """The many-to-one case, which the runtime verifies rather than trusts."""
    kept = _all_filings()
    repeated = kept["client"].value_counts()

    assert int(repeated.max()) > 1, "no client files twice, so m:1 is never exercised"
    assert len(_joined()) == len(kept)


def test_an_unmatched_filing_survives_with_a_blank_commitment():
    """The non-match record: the filing's own fields are all still there."""
    joined = _joined()
    unmatched = joined[joined["public_commitment"].isna()]

    assert len(unmatched) == _UNMATCHED
    assert unmatched["commitment_source"].isna().all()
    for column in ("filing_id", "client", "amount_usd", "specific_issues"):
        assert unmatched[column].notna().all()


def test_a_repeated_commitment_row_fails_the_run_rather_than_multiplying_filings():
    stage = _stage(_load_fixture(), "matched_commitments")
    commitments = pd.read_csv(_COMMITMENTS_PATH)
    doubled = pd.concat([commitments, commitments.head(1)], ignore_index=True)

    with pytest.raises(ValueError, match="public_commitments"):
        _execute(
            stage,
            {"raw_filings": _all_filings(), "public_commitments": doubled},
        )


# ── the flag ─────────────────────────────────────────────────────────────────


def test_flag_contradiction_is_grain_preserving():
    stage = _stage(_load_fixture(), "flag_contradiction")
    judged = _stand_in_for_the_model(_joined())

    flagged = _execute(stage, {"judge_alignment": judged})

    assert len(flagged) == len(judged) == _ROWS_IN_CSV
    assert list(flagged["filing_id"]) == list(judged["filing_id"])
    assert flagged["contradicts_commitment"].notna().all()
    assert set(flagged["contradicts_commitment"].map(type)) == {bool}


def test_flag_contradiction_flags_only_a_judged_contradiction_of_a_matched_commitment():
    stage = _stage(_load_fixture(), "flag_contradiction")
    judged = _stand_in_for_the_model(_joined())

    flagged = _execute(stage, {"judge_alignment": judged})

    expected = [
        (not pd.isna(said)) and alignment == _CONTRADICTS
        for said, alignment in zip(judged["public_commitment"], judged["alignment"])
    ]
    assert list(flagged["contradicts_commitment"]) == expected
    assert any(expected)


def test_a_filing_with_no_commitment_on_record_is_never_flagged():
    """Code decides this, not the model — whatever the model answered for that row."""
    stage = _stage(_load_fixture(), "flag_contradiction")
    judged = _stand_in_for_the_model(_joined())
    judged.loc[judged["public_commitment"].isna(), "alignment"] = _CONTRADICTS

    flagged = _execute(stage, {"judge_alignment": judged})

    unmatched = flagged[flagged["public_commitment"].isna()]
    assert len(unmatched) == _UNMATCHED
    assert not unmatched["contradicts_commitment"].any()


def test_flag_contradiction_refuses_a_matched_filing_the_model_left_unjudged():
    stage = _stage(_load_fixture(), "flag_contradiction")
    judged = _stand_in_for_the_model(_joined())
    judged.loc[judged["public_commitment"].notna(), "alignment"] = None

    with pytest.raises(StepRefused, match="no judgment"):
        _execute(stage, {"judge_alignment": judged})


# ── the model step ───────────────────────────────────────────────────────────


def test_judge_alignment_reads_a_dozen_filings_per_model_call():
    judge = _stage(_load_fixture(), "judge_alignment")
    # Each call spawns a process, so the batch size is the tour's wall clock.

    assert judge.llm is not None
    assert judge.llm.batch_size == _BATCH_SIZE
    assert _ROWS_IN_CSV <= _BATCH_SIZE * 2
    for placeholder in ("{client}", "{public_commitment}", "{specific_issues}"):
        assert placeholder in judge.llm.prompt_data_template


def test_the_model_is_shown_both_texts_and_told_to_use_nothing_else():
    """The organisations are invented, so any outside knowledge would be fabricated."""
    judge = _stage(_load_fixture(), "judge_alignment")
    assert judge.llm is not None
    instructions = judge.llm.prompt_instructions

    assert "Judge only the two texts in front of you" in instructions
    assert "never invent, complete or paraphrase a commitment" in instructions
    # The join leaves this blank for an unmatched filing, and pandas renders it `nan`.
    assert "No commitment given" in instructions and "`nan`" in instructions


def test_the_methodology_document_states_the_batch_size_the_stage_uses():
    wf = _load_fixture()
    # The document is the source of record a reviewer reads against the stage.

    assert "twelve at a time in one model call" in wf.document
    assert "four at a time" not in wf.document


def test_the_methodology_document_admits_the_data_is_invented():
    wf = _load_fixture()

    assert "The sample data is INVENTED." in wf.document
    assert "no row describes a real filing, client, firm or commitment" in wf.document


def test_the_first_six_filings_are_one_model_call():
    # What the tour's small run costs: beat 2 caps raw_filings at 6 rows.
    df = pd.read_csv(_CSV_PATH).head(_TOUR_LIMIT)

    assert len(df) <= _BATCH_SIZE


# ── the review guide ─────────────────────────────────────────────────────────


def test_the_committed_review_guide_accounts_for_every_stage():
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )

    narrated = [stage_id for step in guide.steps for stage_id in step.stage_ids]
    assert narrated == _EXPECTED_STAGE_IDS
    assert guide.unnarrated == []
    for step in guide.steps:
        assert step.data_description and step.data_description.strip()


def test_the_review_guide_keeps_every_check_without_the_padding():
    """Each step is capped, and each capped step still names what a reviewer must check."""
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )

    over = [step.title for step in guide.steps if len(step.prose) > _GUIDE_PROSE_CEILING]
    assert not over, over
    prose = " ".join(step.prose for step in guide.steps)
    for check in (
        "Invented",
        "a repeat fails the run",
        "the absence IS the record",
        "Trust this step least",
        "only where a commitment was joined",
        # The guide ends where the run does: on the file it published.
        "open it under Published",
        "linked back to its own lineage",
    ):
        assert check in prose, check


# ── the published report ─────────────────────────────────────────────────────


def _publish_a_report(tmp_path, df: pd.DataFrame) -> str:
    stage = _stage(_bound_fixture(), "publish_report")
    # A project-scoped context, because the step declares `trace_links` — the run's
    # (project, run_id) is what a row-trace URL is built from.
    ctx = RunContext.for_workflow_test_run(tmp_path, tmp_path, "tutorial", "R-1")
    out = HANDLERS[StageType(stage.type)].execute(stage, {"flag_contradiction": df}, ctx)
    assert out is not None
    return Path(out.iloc[0]["report_path"]).read_text(encoding="utf-8")


def _bound_fixture() -> WorkflowFile:
    """The fixture as committed — it needs no filling in to be a valid document."""
    return WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _three_filings() -> pd.DataFrame:
    """One contradiction, one match, one filing the join found no commitment for."""
    return pd.DataFrame(
        [
            {
                "filing_id": "F-1", "client": "Promise Breakers Mutual", "registrant": "Firm A",
                "amount_usd": 250000, "filing_period": "2024 Q1",
                "specific_issues": "Opposing the national clean electricity standard.",
                "public_commitment": "Publicly committed to supporting a national clean electricity standard.",
                "commitment_source": "2023 climate pledge",
                "alignment": "Contradicts",
                "alignment_note": "Said it backed the standard; this filing opposes it.",
                "contradicts_commitment": True,
            },
            {
                "filing_id": "F-2", "client": "Consistent Cooperative", "registrant": "Firm B",
                "amount_usd": 60000, "filing_period": "2024 Q1",
                "specific_issues": "Supporting faster offshore wind lease sales.",
                "public_commitment": "Publicly committed to building out offshore wind capacity.",
                "commitment_source": "2024 investor letter",
                "alignment": "Matches",
                "alignment_note": "Said it would build offshore wind; this filing asks for that.",
                "contradicts_commitment": False,
            },
            {
                "filing_id": "F-3", "client": "Silent Holdings", "registrant": "Firm C",
                "amount_usd": 90000, "filing_period": "2024 Q1",
                "specific_issues": "Seeking shorter permitting timelines.",
                "public_commitment": None, "commitment_source": None,
                "alignment": "No commitment given", "alignment_note": None,
                "contradicts_commitment": False,
            },
        ]
    )


def test_the_report_shows_both_sides_of_a_contradiction(tmp_path):
    """A bare flag is not an argument: print what they said and what they lobbied for."""
    page = _publish_a_report(tmp_path, _three_filings())

    assert "Said" in page and "Lobbied for" in page
    assert "Publicly committed to supporting a national clean electricity standard." in page
    assert "Opposing the national clean electricity standard." in page
    assert "$250,000" in page
    # Both texts sit in the follow-up cell, in that order, so the cell stands alone.
    said_at = page.index("<span class=\"label\">Said</span>")
    assert page.index("<span class=\"label\">Lobbied for</span>") > said_at


def test_the_report_prints_no_contradiction_for_a_filing_that_matches(tmp_path):
    page = _publish_a_report(tmp_path, _three_filings().iloc[[1]])

    assert "Lobbied for" not in page
    assert "Matches" in page
    assert "1 filings; 0 ask government for the opposite" in page


def test_the_report_says_when_no_commitment_was_on_record(tmp_path):
    """The join's non-match is on the page, not only in lineage."""
    page = _publish_a_report(tmp_path, _three_filings().iloc[[2]])

    assert "No public commitment on record" in page
    assert "1 have no public commitment on record" in page
    assert "Lobbied for" not in page


def test_the_report_scores_nothing_and_recommends_nothing(tmp_path):
    page = _publish_a_report(tmp_path, _three_filings())

    assert "reading aid, not a verdict" in page
    for verdict_word in ("score", "rank", "recommend", "priority"):
        assert verdict_word not in page.lower(), verdict_word


def test_the_report_admits_the_data_is_invented(tmp_path):
    page = _publish_a_report(tmp_path, _three_filings())

    assert "Invented sample data bundled with this tutorial." in page
    assert "No row describes a real filing, client, firm or commitment." in page


def test_every_report_row_links_back_to_the_row_it_came_from(tmp_path):
    """A published claim nobody can trace is the thing this product exists against."""
    page = _publish_a_report(tmp_path, _three_filings())

    for ordinal in range(len(_three_filings())):
        assert f'href="/project/tutorial/runs/R-1/stage/flag_contradiction/row/{ordinal}' \
               '/trace/view">Lineage</a>' in page


def test_the_report_step_refuses_a_flag_it_cannot_print_both_sides_of(tmp_path):
    """Flagged yet carrying no commitment: there is no other side to show."""
    df = _three_filings().iloc[[2]].assign(contradicts_commitment=True)

    with pytest.raises(StepRefused, match="cannot say what it contradicts"):
        _publish_a_report(tmp_path, df)


# ── the template is a file, not a string in the code ─────────────────────────




