from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from evals.harness.binding import ResolvedEval, bind_eval
from evals.harness.cli import main
from evals.harness.definition import CaseRefused, EvalDefinition, Judgement
from evals.harness.rulings import (
    Disagreement,
    RulingNotJudged,
    RulingsInvalid,
    find_ruling_disagreements,
)
from fixed_output_eval import (
    FIXED_OUTPUT_EVAL,
    AnswerInput,
    AnswerOutput,
    ExpectedAnswer,
    judge_answer,
)

FixedOutputEval = EvalDefinition[AnswerInput, ExpectedAnswer, AnswerOutput]


def test_rulings_list_every_disagreement_between_person_and_judge(tmp_path: Path) -> None:
    path = write_rulings(
        tmp_path,
        build_ruling("agreed", "yes", answer=("yes", "matched")),
        build_ruling("disagreed", "no", answer=("yes", "matched")),
        build_ruling("half_disagreed", "no", answer=("yes", "differed"), echo=("no", "differed")),
    )

    assert find_ruling_disagreements(FIXED_OUTPUT_EVAL, path) == [
        Disagreement(
            ruling_id="disagreed",
            key="answer",
            person="matched",
            judge="differed",
            note="answer 'no' is not 'yes'",
        ),
        Disagreement(
            ruling_id="half_disagreed",
            key="echo",
            person="differed",
            judge="matched",
            note="answer 'no' equals 'no'",
        ),
    ]


def test_a_ruling_the_judge_settles_the_persons_way_on_every_key_is_no_disagreement(
    tmp_path: Path,
) -> None:
    path = write_rulings(
        tmp_path,
        build_ruling("agreed", "yes", answer=("yes", "matched"), echo=("no", "differed")),
    )

    assert find_ruling_disagreements(FIXED_OUTPUT_EVAL, path) == []


@pytest.mark.parametrize(
    ("answer", "person", "exit_code", "printed"),
    [
        (
            "no",
            "matched",
            1,
            "ruled answer: person matched, judge differed: answer 'no' is not 'yes'\n",
        ),
        ("yes", "matched", 0, ""),
    ],
    ids=["disagreement", "agreement"],
)
def test_rulings_exit_nonzero_on_disagreement_and_zero_without(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    person: str,
    exit_code: int,
    printed: str,
) -> None:
    eval_dir = tmp_path / FIXED_OUTPUT_EVAL.name
    eval_dir.mkdir()
    write_rulings(eval_dir, build_ruling("ruled", answer, answer=("yes", person)))

    assert (
        main(
            ["rulings", FIXED_OUTPUT_EVAL.name],
            evals_root=tmp_path,
            resolve_eval=resolve_fixed_output,
        )
        == exit_code
    )
    assert capsys.readouterr().out == printed


def test_every_ruling_the_evals_models_refuse_is_named_by_its_ruling_id(tmp_path: Path) -> None:
    path = write_json(
        tmp_path,
        [
            {
                "ruling_id": "unreadable_output",
                "output": {"answer": 7},
                "expected_outputs": [{"key": "answer", "equals": "yes"}],
                "outcomes": [{"key": "answer", "outcome": "matched"}],
            },
            {
                "ruling_id": "unreadable_expected",
                "output": {"answer": "yes"},
                "expected_outputs": [{"key": "answer"}],
                "outcomes": [{"key": "answer", "outcome": "matched"}],
            },
            build_ruling("well_formed", "yes", answer=("yes", "matched")),
        ],
    )

    message = read_refusal(path)

    assert "ruling 'unreadable_output' output.answer: Input should be a valid string" in message
    assert "ruling 'unreadable_expected' expected_outputs[0].equals: Field required" in message
    assert "'well_formed'" not in message


def test_a_ruling_without_a_readable_ruling_id_is_named_by_its_position(tmp_path: Path) -> None:
    path = write_json(
        tmp_path,
        [
            build_ruling("first", "yes", answer=("yes", "matched")),
            {
                "output": {"answer": "yes"},
                "expected_outputs": [{"key": "answer", "equals": "yes"}],
                "outcomes": [{"key": "answer", "outcome": "matched"}],
            },
        ],
    )

    assert read_refusal(path) == f"{path}: rulings[1] ruling_id: Field required"


def test_a_repeated_ruling_id_is_refused_before_any_ruling_is_read(tmp_path: Path) -> None:
    path = write_json(
        tmp_path,
        [
            build_ruling("twice", "yes", answer=("yes", "matched")),
            build_ruling("once", "yes", answer=("yes", "matched")),
            {
                "ruling_id": "twice",
                "output": {"answer": 7},
                "expected_outputs": [{"key": "answer", "equals": "yes"}],
                "outcomes": [{"key": "answer", "outcome": "matched"}],
            },
        ],
    )

    assert read_refusal(path) == f"{path}: ruling_id 'twice' is repeated"


def test_a_field_the_ruling_shape_does_not_define_is_refused(tmp_path: Path) -> None:
    path = write_json(
        tmp_path,
        [
            {
                "ruling_id": "annotated",
                "output": {"answer": "yes"},
                "expected_outputs": [{"key": "answer", "equals": "yes"}],
                "outcomes": [{"key": "answer", "outcome": "matched"}],
                "note": "stray",
            }
        ],
    )

    assert read_refusal(path) == f"{path}: ruling 'annotated' note: Extra inputs are not permitted"


def test_an_outcome_outside_the_evals_outcomes_is_refused(tmp_path: Path) -> None:
    path = write_json(
        tmp_path,
        [
            {
                "ruling_id": "undecided",
                "output": {"answer": "yes"},
                "expected_outputs": [{"key": "answer", "equals": "yes"}],
                "outcomes": [{"key": "answer", "outcome": "unsure"}],
            }
        ],
    )

    assert read_refusal(path) == (
        f"{path}: ruling 'undecided' rules outcome 'unsure' for key 'answer', "
        "not one of 'matched', 'differed'"
    )


@pytest.mark.parametrize(
    ("outcomes", "problem"),
    [
        (
            [{"key": "answer", "outcome": "matched"}],
            "ruling 'partial' expects key 'echo' but rules on no outcome for it",
        ),
        (
            [
                {"key": "answer", "outcome": "matched"},
                {"key": "echo", "outcome": "matched"},
                {"key": "tone", "outcome": "matched"},
            ],
            "ruling 'partial' rules on key 'tone', which it does not expect",
        ),
        (
            [
                {"key": "answer", "outcome": "matched"},
                {"key": "answer", "outcome": "differed"},
                {"key": "echo", "outcome": "matched"},
            ],
            "ruling 'partial' rules on key 'answer' more than once",
        ),
    ],
    ids=["key_without_an_outcome", "outcome_for_an_unexpected_key", "key_ruled_twice"],
)
def test_a_ruling_whose_outcomes_do_not_match_its_expected_keys_is_refused(
    tmp_path: Path, outcomes: JsonValue, problem: str
) -> None:
    path = write_json(
        tmp_path,
        [
            {
                "ruling_id": "partial",
                "output": {"answer": "yes"},
                "expected_outputs": [
                    {"key": "answer", "equals": "yes"},
                    {"key": "echo", "equals": "yes"},
                ],
                "outcomes": outcomes,
            }
        ],
    )

    assert read_refusal(path) == f"{path}: {problem}"


def test_an_expected_key_repeated_within_a_ruling_is_refused(tmp_path: Path) -> None:
    path = write_json(
        tmp_path,
        [
            {
                "ruling_id": "repeats_a_key",
                "output": {"answer": "yes"},
                "expected_outputs": [
                    {"key": "answer", "equals": "yes"},
                    {"key": "answer", "equals": "no"},
                ],
                "outcomes": [{"key": "answer", "outcome": "matched"}],
            }
        ],
    )

    assert read_refusal(path) == (
        f"{path}: ruling 'repeats_a_key' lists expected key 'answer' more than once"
    )


def test_a_ruling_expecting_nothing_is_refused(tmp_path: Path) -> None:
    path = write_json(
        tmp_path,
        [
            {
                "ruling_id": "expects_nothing",
                "output": {"answer": "yes"},
                "expected_outputs": [],
                "outcomes": [],
            }
        ],
    )

    assert read_refusal(path) == f"{path}: ruling 'expects_nothing' has no expected outputs"


def test_a_file_that_is_not_a_list_of_rulings_is_refused(tmp_path: Path) -> None:
    path = write_json(tmp_path, {"rulings": []})

    assert read_refusal(path) == f"{path}: rulings: Input should be a valid array"


def test_a_file_holding_no_rulings_is_refused(tmp_path: Path) -> None:
    path = write_json(tmp_path, [])

    assert read_refusal(path) == f"{path}: no rulings to compare"


def test_a_ruling_the_judge_refuses_is_reported_with_the_judges_reason(tmp_path: Path) -> None:
    path = write_rulings(
        tmp_path,
        build_ruling("declines", "no comment", answer=("yes", "differed")),
    )
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_unless_declined)

    assert read_judging_refusal(path, definition) == (
        f"{path}: ruling 'declines': the judge refused: the output declines to answer"
    )


def test_a_judgement_missing_a_key_the_ruling_rules_on_is_refused(tmp_path: Path) -> None:
    path = write_ruled_rulings(tmp_path)
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_without_echo)

    assert read_judging_refusal(path, definition) == (
        f"{path}: ruling 'ruled': the judge gave no outcome for key 'echo'"
    )


def test_a_judgement_of_a_key_the_ruling_does_not_rule_on_is_refused(tmp_path: Path) -> None:
    path = write_ruled_rulings(tmp_path)
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_with_an_unexpected_key)

    assert read_judging_refusal(path, definition) == (
        f"{path}: ruling 'ruled': the judge judged key 'tone', "
        "which the ruling does not rule on"
    )


def test_a_judgement_repeating_a_key_the_ruling_rules_on_is_refused(tmp_path: Path) -> None:
    path = write_ruled_rulings(tmp_path)
    definition = dataclasses.replace(FIXED_OUTPUT_EVAL, judge=judge_twice)

    assert read_judging_refusal(path, definition) == (
        f"{path}: ruling 'ruled': the judge judged key 'answer' more than once"
    )


def write_ruled_rulings(tmp_path: Path) -> Path:
    return write_rulings(
        tmp_path, build_ruling("ruled", "yes", answer=("yes", "matched"), echo=("yes", "matched"))
    )


def resolve_fixed_output(name: str) -> ResolvedEval:
    return bind_eval(FIXED_OUTPUT_EVAL)


def write_rulings(tmp_path: Path, *rulings: JsonValue) -> Path:
    return write_json(tmp_path, list(rulings))


def write_json(tmp_path: Path, document: JsonValue) -> Path:
    path = tmp_path / "rulings.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def build_ruling(ruling_id: str, answer: str, /, **expected: tuple[str, str]) -> JsonValue:
    return {
        "ruling_id": ruling_id,
        "output": {"answer": answer},
        "expected_outputs": [
            {"key": key, "equals": equals} for key, (equals, _) in expected.items()
        ],
        "outcomes": [{"key": key, "outcome": outcome} for key, (_, outcome) in expected.items()],
    }


def read_refusal(path: Path, definition: FixedOutputEval = FIXED_OUTPUT_EVAL) -> str:
    with pytest.raises(RulingsInvalid) as refusal:
        find_ruling_disagreements(definition, path)
    return str(refusal.value)


def read_judging_refusal(path: Path, definition: FixedOutputEval) -> str:
    with pytest.raises(RulingNotJudged) as refusal:
        find_ruling_disagreements(definition, path)
    return str(refusal.value)


def judge_unless_declined(
    output: AnswerOutput, expected_outputs: list[ExpectedAnswer]
) -> list[Judgement]:
    if output.answer == "no comment":
        raise CaseRefused("the output declines to answer")
    return judge_answer(output, expected_outputs)


def judge_without_echo(
    output: AnswerOutput, expected_outputs: list[ExpectedAnswer]
) -> list[Judgement]:
    return [
        judgement for judgement in judge_answer(output, expected_outputs) if judgement.key != "echo"
    ]


def judge_with_an_unexpected_key(
    output: AnswerOutput, expected_outputs: list[ExpectedAnswer]
) -> list[Judgement]:
    tone = Judgement(key="tone", outcome="matched", note="the tone is neutral")
    return [*judge_answer(output, expected_outputs), tone]


def judge_twice(output: AnswerOutput, expected_outputs: list[ExpectedAnswer]) -> list[Judgement]:
    judgements = judge_answer(output, expected_outputs)
    return [judgements[0], *judgements]
