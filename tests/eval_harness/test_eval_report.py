from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel

from evals.harness import orchestration
from evals.harness.definition import (
    CaseRefused,
    EvalDefinition,
    Judgement,
    LoadContext,
    Loaded,
)
from evals.harness.orchestration import judge_pass, run_pass
from evals.harness.passes import PassIncomplete
from evals.harness.report import (
    AttemptOutcome,
    CaseNotInDataset,
    CaseSection,
    EarlierPassInvalid,
    ExpectedRow,
    JudgementMissing,
    JudgementOutcomeUnknown,
    OutcomeCount,
    PassReport,
    build_pass_report,
    write_pass_report,
)
from fixed_output_eval import (
    FIXED_OUTPUT_EVAL,
    AnswerInput,
    AnswerOutput,
    ExpectedAnswer,
    judge_answer,
)
from test_eval_passes import build_case, load_until_broken, read_head_commit, write_dataset

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIRST_PASS_ID = "20260915T120000"
_SECOND_PASS_ID = "20260915T120100"
_SCORE_WORDS = ("total", "percent", "score", "ratio", "rate")


def test_the_report_counts_each_outcome_per_expected_output(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("sometimes", "yes", answer="yes"),
        build_case("never", "maybe", answer="other"),
    )
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=build_flipping_loader(clock, 0.25))
    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=3, confirmed_loads=6, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    assert report.cases == [
        CaseSection(
            case_id="sometimes",
            rows=[
                ExpectedRow(
                    key="answer",
                    outcome_by_attempt=[
                        AttemptOutcome(attempt=1, outcome="matched"),
                        AttemptOutcome(attempt=2, outcome="differed"),
                        AttemptOutcome(attempt=3, outcome="matched"),
                    ],
                    counts=[
                        OutcomeCount(outcome="matched", count=2),
                        OutcomeCount(outcome="differed", count=1),
                    ],
                    judged=3,
                    latest_note="answer 'yes' equals 'yes'",
                    earlier_counts=None,
                )
            ],
            refusals=[],
        ),
        CaseSection(
            case_id="never",
            rows=[
                ExpectedRow(
                    key="answer",
                    outcome_by_attempt=[
                        AttemptOutcome(attempt=1, outcome="differed"),
                        AttemptOutcome(attempt=2, outcome="differed"),
                        AttemptOutcome(attempt=3, outcome="differed"),
                    ],
                    counts=[
                        OutcomeCount(outcome="matched", count=0),
                        OutcomeCount(outcome="differed", count=3),
                    ],
                    judged=3,
                    latest_note="answer 'maybe' is not 'other'",
                    earlier_counts=None,
                )
            ],
            refusals=[],
        ),
    ]


def test_the_report_shows_an_earlier_pass_beside_when_asked(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("stays", "yes", answer="yes"))
    earlier_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=2, confirmed_loads=2, case_ids=[],
        repo_root=_REPO_ROOT,
    )
    clock.move_on(60)
    write_dataset(
        eval_dir,
        build_case("stays", "yes", answer="yes", echo="no"),
        build_case("added", "yes", answer="yes"),
    )
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=2, confirmed_loads=4, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir, against=earlier_dir)

    matched_twice = [
        OutcomeCount(outcome="matched", count=2),
        OutcomeCount(outcome="differed", count=0),
    ]
    assert report.pass_id == _SECOND_PASS_ID
    assert report.against_pass_id == _FIRST_PASS_ID
    assert read_earlier_counts(report) == {
        ("stays", "answer"): matched_twice,
        ("stays", "echo"): None,
        ("added", "answer"): None,
    }
    page = render_page(report, pass_dir)
    assert "<th>earlier counts</th>" in page
    assert f"<li>beside pass: {_FIRST_PASS_ID}</li>" in page
    assert "<td>—</td>" in page


@pytest.mark.parametrize(
    ("cost_by_answer", "cost_usd", "loads_without_cost", "stated"),
    [
        ({"yes": 0.25, "no": 0.75}, 2.0, 0, "<li>cost $2.000000</li>"),
        ({"yes": 0.25, "no": None}, None, 2, "<li>cost not reported for 2 loads</li>"),
    ],
    ids=["every_load_reported_one", "two_loads_reported_none"],
)
def test_total_cost_is_stated_only_when_every_load_reported_one(
    tmp_path: Path,
    clock: MovableClock,
    cost_by_answer: dict[str, float | None],
    cost_usd: float | None,
    loads_without_cost: int,
    stated: str,
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("priced", "yes", answer="yes"),
        build_case("unpriced", "no", answer="no"),
    )
    definition = dataclasses.replace(
        FIXED_OUTPUT_EVAL, load=build_priced_loader(cost_by_answer)
    )
    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=2, confirmed_loads=4, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    assert report.cost_usd == cost_usd
    assert report.loads_without_cost == loads_without_cost
    assert stated in render_page(report, pass_dir)


def test_a_load_refusal_does_not_leave_the_cost_unstated(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("declines", "no comment", answer="yes"),
        build_case("priced", "yes", answer="yes"),
    )
    definition = dataclasses.replace(
        FIXED_OUTPUT_EVAL, load=build_declining_loader(0.25, "the source declined to comment")
    )
    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=2, confirmed_loads=4, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    assert report.loads_refused == 1
    assert report.loads_started == 3
    assert report.cost_usd == 0.5
    assert report.loads_without_cost == 0


def test_a_pass_whose_every_case_refused_states_no_cost_rather_than_zero(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("declines", "no comment", answer="yes"))
    definition = dataclasses.replace(
        FIXED_OUTPUT_EVAL, load=build_declining_loader(0.25, "the source declined to comment")
    )
    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=2, confirmed_loads=2, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    assert report.cost_usd is None
    assert report.loads_without_cost == 0
    assert "<li>cost not reported for 0 loads</li>" in render_page(report, pass_dir)


def test_the_page_states_a_short_pass_and_a_small_cost_without_rounding_them_to_zero(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=build_slow_cheap_loader(clock))
    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=1, confirmed_loads=1, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    page = render_page(report, pass_dir)
    assert (report.seconds, report.cost_usd) == (0.04, 0.000005)
    assert "<li>seconds: 0.040</li>" in page
    assert "<li>cost $0.000005</li>" in page


def test_the_report_lists_each_load_and_judgement_refusal_with_its_attempt_and_step(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir, definition = write_pass_with_refusals(tmp_path, clock)
    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=3, confirmed_loads=6, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    assert {case.case_id: case.refusals for case in report.cases} == {
        "flips": ["attempt 2 judgement refused: the output was flipped"],
        "declines": ["attempt 1 load refused: the source declined to comment"],
    }


def test_an_outcome_keeps_its_attempt_number_when_an_earlier_judgement_was_refused(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir, definition = write_pass_with_refusals(tmp_path, clock)
    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=3, confirmed_loads=6, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    assert report.cases[0].rows == [
        ExpectedRow(
            key="answer",
            outcome_by_attempt=[
                AttemptOutcome(attempt=1, outcome="matched"),
                AttemptOutcome(attempt=3, outcome="matched"),
            ],
            counts=[
                OutcomeCount(outcome="matched", count=2),
                OutcomeCount(outcome="differed", count=0),
            ],
            judged=2,
            latest_note="answer 'yes' equals 'yes'",
            earlier_counts=None,
        )
    ]


def test_an_expected_output_whose_attempts_went_unjudged_shows_no_outcome_and_no_note(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("declines", "no comment", answer="yes"))
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=refuse_to_judge_the_flipped)
    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=2, confirmed_loads=2, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    assert report.cases[0].rows == [
        ExpectedRow(
            key="answer",
            outcome_by_attempt=[],
            counts=[
                OutcomeCount(outcome="matched", count=0),
                OutcomeCount(outcome="differed", count=0),
            ],
            judged=0,
            latest_note=None,
            earlier_counts=None,
        )
    ]
    assert "<td>—</td>" in render_page(report, pass_dir)


def test_report_json_holds_what_the_page_shows(tmp_path: Path, clock: MovableClock) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=1, confirmed_loads=1, case_ids=[],
        repo_root=_REPO_ROOT,
    )
    report = build_pass_report(FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir, against=None)

    write_pass_report(report, pass_dir)

    stored = PassReport.model_validate_json(
        (pass_dir / "report.json").read_text(encoding="utf-8")
    )
    page = (pass_dir / "report.html").read_text(encoding="utf-8")
    assert stored == report
    assert "<li>eval: fixed_output</li>" in page
    assert f"<li>pass: {_FIRST_PASS_ID}</li>" in page
    assert f"<li>code commit: {read_head_commit(_REPO_ROOT)}</li>" in page
    assert "<li>repeats: 1</li>" in page
    assert "<li>loads started: 1</li>" in page
    assert "<li>loads refused: 0</li>" in page
    assert "<li>seconds: 0.000</li>" in page
    assert "<li>cost $0.010000</li>" in page
    assert "<h2>agrees</h2>" in page
    assert "<th>expected key</th><th>attempt 1</th><th>counts</th><th>latest note</th>" in page
    assert "<td>answer</td><td>matched</td><td>matched 1, differed 0</td>" in page
    assert "<td>answer &#x27;yes&#x27; equals &#x27;yes&#x27;</td>" in page
    assert "<th>earlier counts</th>" not in page


def test_report_html_escapes_what_an_eval_wrote(tmp_path: Path, clock: MovableClock) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("marked", "yes", **{"<b>key</b>": "yes"}),
        build_case("declines", "no comment", answer="yes"),
    )
    definition = dataclasses.replace(
        FIXED_OUTPUT_EVAL,
        load=build_declining_loader(0.25, "<i>declined</i> & left"),
        judge=judge_with_marked_up_notes,
    )
    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=1, confirmed_loads=2, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    page = render_page(report, pass_dir)
    assert "<td>&lt;b&gt;key&lt;/b&gt;</td>" in page
    assert "&lt;script&gt;alert(&#x27;note&#x27;)&lt;/script&gt;" in page
    assert "attempt 1 load refused: &lt;i&gt;declined&lt;/i&gt; &amp; left" in page
    assert "<script>" not in page
    assert "<b>" not in page
    assert "<i>" not in page


def test_the_report_and_the_pages_chrome_hold_no_total_or_percentage(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=2, confirmed_loads=2, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    report = build_pass_report(FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir, against=None)

    chrome = read_chrome_text(render_page(report, pass_dir)).lower()
    assert "loads started" in chrome and "expected key" in chrome
    assert [name for name in find_field_names(PassReport) if "count" in name]
    assert not [name for name in find_field_names(PassReport) if _names_a_score(name)]
    assert not [word for word in _SCORE_WORDS if word in chrome]
    assert "%" not in chrome


def test_the_report_refuses_a_pass_whose_loading_stopped(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("agrees", "yes", answer="yes"),
        build_case("breaks", "boom", answer="yes"),
    )
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=load_until_broken)
    with pytest.raises(KeyError, match="boom"):
        run_pass(
            definition, eval_dir=eval_dir, repeats=1, confirmed_loads=2, case_ids=[],
            repo_root=_REPO_ROOT,
        )
    pass_dir = eval_dir / "passes" / _FIRST_PASS_ID

    with pytest.raises(PassIncomplete) as refusal:
        build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None)

    assert str(refusal.value) == (
        f"{pass_dir}: loading stopped before recording case 'breaks' attempt 1"
    )


def test_the_report_refuses_a_stored_output_no_one_judged(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=2, confirmed_loads=2, case_ids=[],
        repo_root=_REPO_ROOT,
    )
    (pass_dir / "judgements" / "agrees" / "2.json").unlink()

    with pytest.raises(JudgementMissing) as refusal:
        build_pass_report(FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir, against=None)

    assert str(refusal.value) == f"{pass_dir}: no judgement for case 'agrees' attempt 2"


def test_the_report_refuses_a_case_the_dataset_no_longer_holds(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("stays", "yes", answer="yes"),
        build_case("gone", "yes", answer="yes"),
    )
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=1, confirmed_loads=2, case_ids=[],
        repo_root=_REPO_ROOT,
    )
    dataset_path = write_dataset(eval_dir, build_case("stays", "yes", answer="yes"))

    with pytest.raises(CaseNotInDataset) as refusal:
        build_pass_report(FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir, against=None)

    assert str(refusal.value) == (
        f"{dataset_path}: the pass ran case_ids not in the dataset: 'gone'"
    )


def test_the_report_refuses_an_earlier_pass_recorded_for_another_eval(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    earlier_dir = run_another_eval_pass(tmp_path)
    clock.move_on(60)
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=1, confirmed_loads=1, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    with pytest.raises(EarlierPassInvalid) as refusal:
        build_pass_report(FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir, against=earlier_dir)

    assert str(refusal.value) == (
        f"{earlier_dir}: pass is for eval 'another_eval', not 'fixed_output'"
    )


def test_the_report_refuses_an_earlier_pass_whose_loading_stopped(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("agrees", "yes", answer="yes"),
        build_case("breaks", "boom", answer="yes"),
    )
    with pytest.raises(KeyError, match="boom"):
        run_pass(
            dataclasses.replace(FIXED_OUTPUT_EVAL, load=load_until_broken),
            eval_dir=eval_dir, repeats=1, confirmed_loads=2, case_ids=[], repo_root=_REPO_ROOT,
        )
    earlier_dir = eval_dir / "passes" / _FIRST_PASS_ID
    clock.move_on(60)
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=1, confirmed_loads=1,
        case_ids=["agrees"], repo_root=_REPO_ROOT,
    )

    with pytest.raises(PassIncomplete) as refusal:
        build_pass_report(FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir, against=earlier_dir)

    assert str(refusal.value) == (
        f"{earlier_dir}: loading stopped before recording case 'breaks' attempt 1"
    )


def test_the_report_refuses_a_stored_judgement_whose_outcome_the_eval_no_longer_lists(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=2, confirmed_loads=2, case_ids=[],
        repo_root=_REPO_ROOT,
    )
    renamed = dataclasses.replace(FIXED_OUTPUT_EVAL, outcomes=("agreed", "differed"))

    with pytest.raises(JudgementOutcomeUnknown) as refusal:
        build_pass_report(renamed, pass_dir, eval_dir=eval_dir, against=None)

    assert str(refusal.value) == (
        f"{pass_dir}: "
        "case 'agrees' attempt 1 key 'answer' has outcome 'matched', "
        "not one of 'agreed', 'differed'; "
        "case 'agrees' attempt 2 key 'answer' has outcome 'matched', "
        "not one of 'agreed', 'differed'"
    )


def test_the_report_refuses_an_earlier_pass_judged_under_outcomes_the_eval_no_longer_lists(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    earlier_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=1, confirmed_loads=1, case_ids=[],
        repo_root=_REPO_ROOT,
    )
    clock.move_on(60)
    renamed = dataclasses.replace(
        FIXED_OUTPUT_EVAL, outcomes=("agreed", "differed"), judge=judge_agreed
    )
    pass_dir = run_pass(
        renamed, eval_dir=eval_dir, repeats=1, confirmed_loads=1, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    with pytest.raises(JudgementOutcomeUnknown) as refusal:
        build_pass_report(renamed, pass_dir, eval_dir=eval_dir, against=earlier_dir)

    assert str(refusal.value) == (
        f"{earlier_dir}: case 'agrees' attempt 1 key 'answer' has outcome 'matched', "
        "not one of 'agreed', 'differed'"
    )


def test_running_a_pass_writes_its_report(tmp_path: Path, clock: MovableClock) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))

    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=2, confirmed_loads=2, case_ids=[],
        repo_root=_REPO_ROOT,
    )

    written = PassReport.model_validate_json(
        (pass_dir / "report.json").read_text(encoding="utf-8")
    )
    assert written == build_pass_report(
        FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir, against=None
    )
    assert written.cases[0].rows[0] == ExpectedRow(
        key="answer",
        outcome_by_attempt=[
            AttemptOutcome(attempt=1, outcome="matched"),
            AttemptOutcome(attempt=2, outcome="matched"),
        ],
        counts=[
            OutcomeCount(outcome="matched", count=2),
            OutcomeCount(outcome="differed", count=0),
        ],
        judged=2,
        latest_note="answer 'yes' equals 'yes'",
        earlier_counts=None,
    )
    assert "<h1>fixed_output pass" in (pass_dir / "report.html").read_text(encoding="utf-8")


def test_judging_a_pass_writes_the_report_for_what_it_just_judged(
    tmp_path: Path, clock: MovableClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL, eval_dir=eval_dir, repeats=1, confirmed_loads=1, case_ids=[],
        repo_root=_REPO_ROOT,
    )
    write_dataset(eval_dir, build_case("agrees", "yes", answer="no"))

    judge_pass(FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir)

    written = PassReport.model_validate_json(
        (pass_dir / "report.json").read_text(encoding="utf-8")
    )
    assert written.cases[0].rows[0].outcome_by_attempt == [
        AttemptOutcome(attempt=1, outcome="differed")
    ]
    assert "<td>differed</td>" in (pass_dir / "report.html").read_text(encoding="utf-8")


@dataclass
class MovableClock:
    moment: datetime = datetime(2026, 9, 15, 12, 0, 0)
    elapsed: float = 0.0

    def now(self) -> datetime:
        return self.moment

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds

    def move_on(self, seconds: int) -> None:
        self.moment += timedelta(seconds=seconds)


LoadFunction = Callable[[AnswerInput, LoadContext], Loaded[AnswerOutput]]

_FLIPPED = "flipped"
_RENAMED_OUTCOMES = {"matched": "agreed", "differed": "differed"}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> MovableClock:
    fake = MovableClock()
    monkeypatch.setattr(orchestration, "datetime", fake)
    monkeypatch.setattr(orchestration, "time", fake)
    return fake


def build_flipping_loader(clock: MovableClock, cost_usd: float | None) -> LoadFunction:
    attempts: Counter[str] = Counter()

    def load(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
        attempts[case_input.answer] += 1
        clock.advance(0.5)
        answered = case_input.answer if attempts[case_input.answer] % 2 else _FLIPPED
        return Loaded(output=AnswerOutput(answer=answered), cost_usd=cost_usd)

    return load


def build_slow_cheap_loader(clock: MovableClock) -> LoadFunction:
    def load(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
        clock.advance(0.04)
        return Loaded(output=AnswerOutput(answer=case_input.answer), cost_usd=0.000005)

    return load


def read_chrome_text(page: str) -> str:
    facts = re.search(r'<ul class="facts">(.*?)</ul>', page, re.DOTALL)
    assert facts is not None, "the page has no facts list"
    return facts.group(1) + " " + " ".join(re.findall(r"<th>(.*?)</th>", page))


def build_priced_loader(cost_by_answer: dict[str, float | None]) -> LoadFunction:
    def load(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
        return Loaded(
            output=AnswerOutput(answer=case_input.answer),
            cost_usd=cost_by_answer[case_input.answer],
        )

    return load


def build_declining_loader(cost_usd: float | None, reason: str) -> LoadFunction:
    def load(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
        if case_input.answer == "no comment":
            raise CaseRefused(reason)
        return Loaded(output=AnswerOutput(answer=case_input.answer), cost_usd=cost_usd)

    return load


def write_pass_with_refusals(
    tmp_path: Path, clock: MovableClock
) -> tuple[Path, EvalDefinition[AnswerInput, ExpectedAnswer, AnswerOutput]]:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("flips", "yes", answer="yes"),
        build_case("declines", "no comment", answer="yes"),
    )
    definition = dataclasses.replace(
        FIXED_OUTPUT_EVAL,
        load=build_flipping_and_declining_loader(clock),
        judge=refuse_to_judge_the_flipped,
    )
    return eval_dir, definition


def build_flipping_and_declining_loader(clock: MovableClock) -> LoadFunction:
    flipping = build_flipping_loader(clock, 0.25)
    declining = build_declining_loader(0.25, "the source declined to comment")

    def load(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
        if case_input.answer == "no comment":
            return declining(case_input, context)
        return flipping(case_input, context)

    return load


def refuse_to_judge_the_flipped(
    output: AnswerOutput, expected_outputs: list[ExpectedAnswer]
) -> list[Judgement]:
    if output.answer in (_FLIPPED, "no comment"):
        raise CaseRefused("the output was flipped")
    return judge_answer(output, expected_outputs)


def judge_agreed(output: AnswerOutput, expected_outputs: list[ExpectedAnswer]) -> list[Judgement]:
    return [
        judgement.model_copy(update={"outcome": _RENAMED_OUTCOMES[judgement.outcome]})
        for judgement in judge_answer(output, expected_outputs)
    ]


def judge_with_marked_up_notes(
    output: AnswerOutput, expected_outputs: list[ExpectedAnswer]
) -> list[Judgement]:
    return [
        Judgement(key=expected.key, outcome="matched", note="<script>alert('note')</script>")
        for expected in expected_outputs
    ]


def run_another_eval_pass(tmp_path: Path) -> Path:
    eval_dir = tmp_path / "another_eval"
    eval_dir.mkdir()
    dataset = {"eval": "another_eval", "cases": [build_case("agrees", "yes", answer="yes")]}
    (eval_dir / "dataset.json").write_text(json.dumps(dataset), encoding="utf-8")
    return run_pass(
        dataclasses.replace(FIXED_OUTPUT_EVAL, name="another_eval"),
        eval_dir=eval_dir, repeats=1, confirmed_loads=1, case_ids=[], repo_root=_REPO_ROOT,
    )


def render_page(report: PassReport, pass_dir: Path) -> str:
    write_pass_report(report, pass_dir)
    return (pass_dir / "report.html").read_text(encoding="utf-8")


def read_earlier_counts(report: PassReport) -> dict[tuple[str, str], list[OutcomeCount] | None]:
    return {
        (case.case_id, row.key): row.earlier_counts
        for case in report.cases
        for row in case.rows
    }


def find_field_names(model: type[BaseModel]) -> set[str]:
    names = set(model.model_fields)
    for declared in model.model_fields.values():
        for nested in find_nested_models(declared.annotation):
            names |= find_field_names(nested)
    return names


def find_nested_models(annotation: object) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for argument in get_args(annotation) for model in find_nested_models(argument)]


def _names_a_score(name: str) -> bool:
    return any(word in name for word in _SCORE_WORDS)
