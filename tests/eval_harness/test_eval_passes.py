from __future__ import annotations

import dataclasses
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import JsonValue, TypeAdapter

from evals.harness import orchestration
from evals.harness.dataset import DatasetInvalid
from evals.harness.definition import CaseRefused, Judgement, LoadContext, Loaded
from evals.harness.orchestration import (
    CaseSelectionInvalid,
    CodeCommitUnreadable,
    JudgementInvalid,
    LoadCountNotConfirmed,
    judge_pass,
    run_pass,
)
from evals.harness.passes import (
    JudgementRefused,
    LoadRefused,
    OutputStored,
    PassFolderExists,
    PassRecord,
)
from fixed_output_eval import (
    FIXED_OUTPUT_EVAL,
    AnswerInput,
    AnswerOutput,
    ExpectedAnswer,
    judge_answer,
    load_answer,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PASS_ID = "20260915T120000"


def test_a_pass_loads_each_case_the_requested_number_of_times_and_stores_every_output(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("agrees", "yes", answer="yes"),
        build_case("disagrees", "no", answer="yes"),
    )
    calls: list[LoadCall] = []
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=build_timed_loader(clock, calls))

    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=2, confirmed_loads=4, case_ids=[], repo_root=_REPO_ROOT
    )

    context = LoadContext(
        pass_dir=pass_dir, workspace_dir=pass_dir / "workspace", repo_root=_REPO_ROOT
    )
    assert calls == [
        LoadCall("yes", context),
        LoadCall("yes", context),
        LoadCall("no", context),
        LoadCall("no", context),
    ]
    assert context.workspace_dir.is_dir()
    assert read_pass_record(pass_dir).loads == [
        OutputStored(case_id="agrees", attempt=1, seconds=1.5, cost_usd=0.01),
        OutputStored(case_id="agrees", attempt=2, seconds=1.5, cost_usd=0.01),
        OutputStored(case_id="disagrees", attempt=1, seconds=0.25, cost_usd=None),
        OutputStored(case_id="disagrees", attempt=2, seconds=0.25, cost_usd=None),
    ]
    assert read_every_output(pass_dir) == {
        "agrees/1": AnswerOutput(answer="yes"),
        "agrees/2": AnswerOutput(answer="yes"),
        "disagrees/1": AnswerOutput(answer="no"),
        "disagrees/2": AnswerOutput(answer="no"),
    }


def test_a_pass_is_named_by_its_start_and_records_its_eval_repeats_and_the_commit_git_reports(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))

    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL,
        eval_dir=eval_dir,
        repeats=1,
        confirmed_loads=1,
        case_ids=[],
        repo_root=_REPO_ROOT,
    )

    assert pass_dir == eval_dir / "passes" / _PASS_ID
    assert read_pass_record(pass_dir).model_dump(exclude={"loads"}) == {
        "eval": "fixed_output",
        "pass_id": _PASS_ID,
        "code_commit": read_head_commit(_REPO_ROOT),
        "repeats": 1,
        "started_at": "2026-09-15T12:00:00",
    }


def test_a_pass_loads_only_the_selected_cases_in_dataset_order(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("first", "yes", answer="yes"),
        build_case("second", "no", answer="no"),
        build_case("third", "maybe", answer="maybe"),
    )
    calls: list[str] = []
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=build_recording_loader(calls))

    pass_dir = run_pass(
        definition,
        eval_dir=eval_dir,
        repeats=1,
        confirmed_loads=2,
        case_ids=["third", "first"],
        repo_root=_REPO_ROOT,
    )

    assert calls == ["yes", "maybe"]
    assert [load.case_id for load in read_pass_record(pass_dir).loads] == ["first", "third"]


def test_a_pass_refuses_to_start_when_the_confirmed_load_count_differs(
    tmp_path: Path, repo_where_git_fails: Path
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("agrees", "yes", answer="yes"),
        build_case("disagrees", "no", answer="yes"),
        build_case("unselected", "yes", answer="yes"),
    )

    with pytest.raises(LoadCountNotConfirmed) as refusal:
        run_pass(
            FIXED_OUTPUT_EVAL,
            eval_dir=eval_dir,
            repeats=3,
            confirmed_loads=9,
            case_ids=["agrees", "disagrees"],
            repo_root=repo_where_git_fails,
        )

    assert str(refusal.value) == "this pass plans 6 loads; confirm with --loads 6"
    assert list_paths(eval_dir) == ["dataset.json"]


def test_an_unknown_case_id_is_refused_before_loading_or_writing_anything(
    tmp_path: Path, repo_where_git_fails: Path
) -> None:
    eval_dir = tmp_path / "fixed_output"
    dataset_path = write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=refuse_to_load)

    with pytest.raises(CaseSelectionInvalid) as refusal:
        run_pass(
            definition,
            eval_dir=eval_dir,
            repeats=1,
            confirmed_loads=3,
            case_ids=["agrees", "missing", "also_missing"],
            repo_root=repo_where_git_fails,
        )

    assert str(refusal.value) == (
        f"{dataset_path}: case_ids not in the dataset: 'missing', 'also_missing'"
    )
    assert list_paths(eval_dir) == ["dataset.json"]


def test_an_invalid_dataset_is_refused_before_anything_is_written(
    tmp_path: Path, repo_where_git_fails: Path
) -> None:
    eval_dir = tmp_path / "fixed_output"
    dataset_path = write_dataset(eval_dir, build_case("expects_nothing", "yes"))

    with pytest.raises(DatasetInvalid) as refusal:
        run_pass(
            FIXED_OUTPUT_EVAL,
            eval_dir=eval_dir,
            repeats=1,
            confirmed_loads=1,
            case_ids=[],
            repo_root=repo_where_git_fails,
        )

    assert str(refusal.value) == f"{dataset_path}: case 'expects_nothing' has no expected outputs"
    assert list_paths(eval_dir) == ["dataset.json"]


def test_a_pass_whose_commit_git_cannot_read_is_refused_before_anything_is_written(
    tmp_path: Path, repo_where_git_fails: Path
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=refuse_to_load)

    with pytest.raises(CodeCommitUnreadable) as refusal:
        run_pass(
            definition,
            eval_dir=eval_dir,
            repeats=1,
            confirmed_loads=1,
            case_ids=[],
            repo_root=repo_where_git_fails,
        )

    assert str(refusal.value).startswith(
        f"git rev-parse HEAD failed in {repo_where_git_fails}: "
        "fatal: ambiguous argument 'HEAD'"
    )
    assert list_paths(eval_dir) == ["dataset.json"]


def test_a_refused_case_is_recorded_and_its_remaining_loads_skipped(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("declines", "no comment", answer="yes"),
        build_case("agrees", "yes", answer="yes"),
    )
    calls: list[str] = []
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=build_declining_loader(calls))

    pass_dir = run_pass(
        definition, eval_dir=eval_dir, repeats=3, confirmed_loads=6, case_ids=[], repo_root=_REPO_ROOT
    )

    assert calls == ["no comment", "yes", "yes", "yes"]
    assert read_pass_record(pass_dir).loads == [
        LoadRefused(case_id="declines", attempt=1, reason="the source declined to comment"),
        OutputStored(case_id="agrees", attempt=1, seconds=0.0, cost_usd=0.01),
        OutputStored(case_id="agrees", attempt=2, seconds=0.0, cost_usd=0.01),
        OutputStored(case_id="agrees", attempt=3, seconds=0.0, cost_usd=0.01),
    ]
    assert list(read_every_output(pass_dir)) == ["agrees/1", "agrees/2", "agrees/3"]


def test_an_unexpected_loading_error_stops_the_pass_and_keeps_what_completed(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("agrees", "yes", answer="yes"),
        build_case("breaks", "boom", answer="yes"),
        build_case("never_loaded", "yes", answer="yes"),
    )
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=load_until_broken)

    with pytest.raises(KeyError, match="boom"):
        run_pass(
            definition,
            eval_dir=eval_dir,
            repeats=2,
            confirmed_loads=6,
            case_ids=[],
            repo_root=_REPO_ROOT,
        )

    pass_dir = eval_dir / "passes" / _PASS_ID
    assert read_pass_record(pass_dir).loads == [
        OutputStored(case_id="agrees", attempt=1, seconds=0.0, cost_usd=0.01),
        OutputStored(case_id="agrees", attempt=2, seconds=0.0, cost_usd=0.01),
    ]
    assert list_paths(pass_dir) == [
        "outputs",
        "outputs/agrees",
        "outputs/agrees/1.json",
        "outputs/agrees/2.json",
        "pass.json",
        "workspace",
    ]


def test_a_pass_folder_that_already_exists_is_refused(tmp_path: Path, clock: FakeClock) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    existing = eval_dir / "passes" / _PASS_ID
    existing.mkdir(parents=True)
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, load=refuse_to_load)

    with pytest.raises(PassFolderExists) as refusal:
        run_pass(
            definition,
            eval_dir=eval_dir,
            repeats=1,
            confirmed_loads=1,
            case_ids=[],
            repo_root=_REPO_ROOT,
        )

    assert str(refusal.value) == f"pass folder {existing} already exists"
    assert list_paths(existing) == []


def test_a_pass_judges_every_stored_output_once_loading_ends(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("agrees", "yes", answer="yes"),
        build_case("disagrees", "no", answer="yes", echo="no"),
    )

    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL,
        eval_dir=eval_dir,
        repeats=2,
        confirmed_loads=4,
        case_ids=[],
        repo_root=_REPO_ROOT,
    )

    agreed = [Judgement(key="answer", outcome="matched", note="answer 'yes' equals 'yes'")]
    disagreed = [
        Judgement(key="answer", outcome="differed", note="answer 'no' is not 'yes'"),
        Judgement(key="echo", outcome="matched", note="answer 'no' equals 'no'"),
    ]
    assert read_every_judgement(pass_dir) == {
        "agrees/1": agreed,
        "agrees/2": agreed,
        "disagrees/1": disagreed,
        "disagrees/2": disagreed,
    }


def test_judging_again_reads_stored_outputs_without_loading_against_the_edited_dataset(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL,
        eval_dir=eval_dir,
        repeats=2,
        confirmed_loads=2,
        case_ids=[],
        repo_root=_REPO_ROOT,
    )
    write_dataset(eval_dir, build_case("agrees", "yes", answer="no"))

    judge_pass(
        dataclasses.replace(FIXED_OUTPUT_EVAL, load=refuse_to_load), pass_dir, eval_dir=eval_dir
    )

    now_differs = [Judgement(key="answer", outcome="differed", note="answer 'yes' is not 'no'")]
    assert read_every_judgement(pass_dir) == {"agrees/1": now_differs, "agrees/2": now_differs}


def test_judging_again_leaves_no_judgement_file_from_an_earlier_judging(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL,
        eval_dir=eval_dir,
        repeats=1,
        confirmed_loads=1,
        case_ids=[],
        repo_root=_REPO_ROOT,
    )
    stale = pass_dir / "judgements" / "gone" / "1.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("[]", encoding="utf-8")

    judge_pass(FIXED_OUTPUT_EVAL, pass_dir, eval_dir=eval_dir)

    assert list_paths(pass_dir / "judgements") == ["agrees", "agrees/1.json"]


def test_a_judgement_missing_an_expected_key_stops_the_pass_before_any_judgement_is_written(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("agrees", "yes", answer="yes"),
        build_case("disagrees", "no", answer="yes", echo="no"),
    )
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_without_echo)

    with pytest.raises(JudgementInvalid) as refusal:
        run_pass(
            definition,
            eval_dir=eval_dir,
            repeats=1,
            confirmed_loads=2,
            case_ids=[],
            repo_root=_REPO_ROOT,
        )

    assert str(refusal.value) == "case 'disagrees' attempt 1: expected key 'echo' has no judgement"
    assert not (eval_dir / "passes" / _PASS_ID / "judgements").exists()


def test_an_outcome_outside_the_evals_outcomes_stops_the_pass(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("disagrees", "no", answer="yes", echo="no"))
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_unsure)

    with pytest.raises(JudgementInvalid) as refusal:
        run_pass(
            definition,
            eval_dir=eval_dir,
            repeats=1,
            confirmed_loads=1,
            case_ids=[],
            repo_root=_REPO_ROOT,
        )

    assert str(refusal.value) == (
        "case 'disagrees' attempt 1: "
        "key 'answer' has outcome 'unsure', not one of 'matched', 'differed'; "
        "key 'echo' has outcome 'unsure', not one of 'matched', 'differed'"
    )


def test_a_judgement_repeating_an_expected_key_stops_the_pass(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_twice)

    with pytest.raises(JudgementInvalid) as refusal:
        run_pass(
            definition,
            eval_dir=eval_dir,
            repeats=1,
            confirmed_loads=1,
            case_ids=[],
            repo_root=_REPO_ROOT,
        )

    assert str(refusal.value) == "case 'agrees' attempt 1: expected key 'answer' is judged 2 times"


def test_a_judgement_for_a_key_the_case_does_not_expect_stops_the_pass(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_with_an_unexpected_key)

    with pytest.raises(JudgementInvalid) as refusal:
        run_pass(
            definition,
            eval_dir=eval_dir,
            repeats=1,
            confirmed_loads=1,
            case_ids=[],
            repo_root=_REPO_ROOT,
        )

    assert str(refusal.value) == (
        "case 'agrees' attempt 1: key 'tone' is not an expected key of the case"
    )


def test_judging_refuses_every_stored_output_whose_case_left_the_dataset_before_judging_any(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    dataset_path = write_dataset(
        eval_dir,
        build_case("stays", "yes", answer="yes"),
        build_case("gone", "yes", answer="yes"),
        build_case("also_gone", "no", answer="no"),
    )
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL,
        eval_dir=eval_dir,
        repeats=2,
        confirmed_loads=6,
        case_ids=[],
        repo_root=_REPO_ROOT,
    )
    write_dataset(eval_dir, build_case("stays", "yes", answer="yes"))

    with pytest.raises(JudgementInvalid) as refusal:
        judge_pass(
            dataclasses.replace(FIXED_OUTPUT_EVAL, judge=refuse_to_judge),
            pass_dir,
            eval_dir=eval_dir,
        )

    assert str(refusal.value) == (
        f"{dataset_path}: stored outputs name case_ids not in the dataset: 'gone', 'also_gone'"
    )


def test_a_judging_that_stops_leaves_the_earlier_judgements_untouched(
    tmp_path: Path, clock: FakeClock
) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("agrees", "yes", answer="yes"),
        build_case("disagrees", "no", answer="yes", echo="no"),
    )
    pass_dir = run_pass(
        FIXED_OUTPUT_EVAL,
        eval_dir=eval_dir,
        repeats=1,
        confirmed_loads=2,
        case_ids=[],
        repo_root=_REPO_ROOT,
    )
    earlier = read_judgement_files(pass_dir)
    assert list(earlier) == ["agrees/1.json", "disagrees/1.json"]
    write_dataset(
        eval_dir,
        build_case("agrees", "yes", answer="no"),
        build_case("disagrees", "no", answer="yes", echo="no"),
    )

    with pytest.raises(JudgementInvalid):
        judge_pass(
            dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_without_echo),
            pass_dir,
            eval_dir=eval_dir,
        )

    assert read_judgement_files(pass_dir) == earlier


def test_a_refused_judgement_is_recorded_with_its_reason(tmp_path: Path, clock: FakeClock) -> None:
    eval_dir = tmp_path / "fixed_output"
    write_dataset(
        eval_dir,
        build_case("declines", "no comment", answer="yes"),
        build_case("agrees", "yes", answer="yes"),
    )
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_unless_declined)

    pass_dir = run_pass(
        definition,
        eval_dir=eval_dir,
        repeats=1,
        confirmed_loads=2,
        case_ids=[],
        repo_root=_REPO_ROOT,
    )

    assert read_every_judgement(pass_dir) == {
        "declines/1": JudgementRefused(reason="the output declines to answer"),
        "agrees/1": [Judgement(key="answer", outcome="matched", note="answer 'yes' equals 'yes'")],
    }


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def now(self) -> datetime:
        return datetime(2026, 9, 15, 12, 0, 0)

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


@dataclass(frozen=True)
class LoadCall:
    answer: str
    context: LoadContext


LoadFunction = Callable[[AnswerInput, LoadContext], Loaded[AnswerOutput]]

_SECONDS_BY_ANSWER = {"yes": 1.5, "no": 0.25}
_COST_BY_ANSWER: dict[str, float | None] = {"yes": 0.01, "no": None}
_JUDGEMENT_FILE: TypeAdapter[list[Judgement] | JudgementRefused] = TypeAdapter(
    list[Judgement] | JudgementRefused
)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(orchestration, "datetime", fake)
    monkeypatch.setattr(orchestration, "time", fake)
    return fake


@pytest.fixture
def repo_where_git_fails(tmp_path: Path) -> Path:
    repo = tmp_path / "repo_without_a_commit"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    return repo


def write_dataset(eval_dir: Path, *cases: JsonValue) -> Path:
    eval_dir.mkdir(parents=True, exist_ok=True)
    path = eval_dir / "dataset.json"
    path.write_text(
        json.dumps({"eval": FIXED_OUTPUT_EVAL.name, "cases": list(cases)}), encoding="utf-8"
    )
    return path


def build_case(case_id: str, answer: str, /, **equals_by_key: str) -> JsonValue:
    return {
        "case_id": case_id,
        "input": {"answer": answer},
        "expected_outputs": [{"key": key, "equals": equals} for key, equals in equals_by_key.items()],
    }


def build_timed_loader(clock: FakeClock, calls: list[LoadCall]) -> LoadFunction:
    def load(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
        calls.append(LoadCall(case_input.answer, context))
        clock.advance(_SECONDS_BY_ANSWER[case_input.answer])
        cost_usd = _COST_BY_ANSWER[case_input.answer]
        return Loaded(output=AnswerOutput(answer=case_input.answer), cost_usd=cost_usd)

    return load


def build_recording_loader(calls: list[str]) -> LoadFunction:
    def load(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
        calls.append(case_input.answer)
        return load_answer(case_input, context)

    return load


def build_declining_loader(calls: list[str]) -> LoadFunction:
    def load(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
        calls.append(case_input.answer)
        if case_input.answer == "no comment":
            raise CaseRefused("the source declined to comment")
        return load_answer(case_input, context)

    return load


def load_until_broken(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
    if case_input.answer == "boom":
        raise KeyError("boom")
    return load_answer(case_input, context)


def refuse_to_load(case_input: AnswerInput, context: LoadContext) -> NoReturn:
    raise AssertionError("this test never loads")


def judge_without_echo(
    output: AnswerOutput, expected_outputs: list[ExpectedAnswer]
) -> list[Judgement]:
    judgements = judge_answer(output, expected_outputs)
    return [judgement for judgement in judgements if judgement.key != "echo"]


def judge_unsure(output: AnswerOutput, expected_outputs: list[ExpectedAnswer]) -> list[Judgement]:
    return [
        Judgement(key=expected.key, outcome="unsure", note="the judge could not decide")
        for expected in expected_outputs
    ]


def judge_twice(output: AnswerOutput, expected_outputs: list[ExpectedAnswer]) -> list[Judgement]:
    judgements = judge_answer(output, expected_outputs)
    return judgements + judgements


def judge_with_an_unexpected_key(
    output: AnswerOutput, expected_outputs: list[ExpectedAnswer]
) -> list[Judgement]:
    tone = Judgement(key="tone", outcome="matched", note="the tone is neutral")
    return [*judge_answer(output, expected_outputs), tone]


def judge_unless_declined(
    output: AnswerOutput, expected_outputs: list[ExpectedAnswer]
) -> list[Judgement]:
    if output.answer == "no comment":
        raise CaseRefused("the output declines to answer")
    return judge_answer(output, expected_outputs)


def refuse_to_judge(output: AnswerOutput, expected_outputs: list[ExpectedAnswer]) -> NoReturn:
    raise AssertionError("this test never judges")


def read_head_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, check=True
    )
    return result.stdout.decode("utf-8").strip()


def read_pass_record(pass_dir: Path) -> PassRecord:
    return PassRecord.model_validate_json((pass_dir / "pass.json").read_text(encoding="utf-8"))


def read_every_output(pass_dir: Path) -> dict[str, AnswerOutput]:
    outputs_dir = pass_dir / "outputs"
    return {
        path.relative_to(outputs_dir).with_suffix("").as_posix(): AnswerOutput.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        for path in sorted(outputs_dir.rglob("*.json"))
    }


def read_every_judgement(pass_dir: Path) -> dict[str, list[Judgement] | JudgementRefused]:
    judgements_dir = pass_dir / "judgements"
    return {
        path.relative_to(judgements_dir).with_suffix("").as_posix(): _JUDGEMENT_FILE.validate_json(
            path.read_text(encoding="utf-8")
        )
        for path in sorted(judgements_dir.rglob("*.json"))
    }


def read_judgement_files(pass_dir: Path) -> dict[str, bytes]:
    judgements_dir = pass_dir / "judgements"
    return {
        path.relative_to(judgements_dir).as_posix(): path.read_bytes()
        for path in sorted(judgements_dir.rglob("*.json"))
    }


def list_paths(folder: Path) -> list[str]:
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*"))
