from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from evals.harness.dataset import Dataset, DatasetInvalid, _is_dataset_of, read_dataset
from fixed_output_eval import FIXED_OUTPUT_EVAL, AnswerInput, AnswerOutput, ExpectedAnswer


def test_a_dataset_parses_each_case_with_the_evals_own_models(tmp_path: Path) -> None:
    path = write_dataset(
        tmp_path,
        FIXED_OUTPUT_EVAL.name,
        [
            {
                "case_id": "agrees",
                "input": {"answer": "yes"},
                "expected_outputs": [{"key": "answer", "equals": "yes"}],
            },
            {
                "case_id": "disagrees",
                "input": {"answer": "no"},
                "expected_outputs": [
                    {"key": "answer", "equals": "yes"},
                    {"key": "echo", "equals": "no"},
                ],
            },
        ],
    )

    dataset = read_dataset(path, FIXED_OUTPUT_EVAL)

    assert [case.case_id for case in dataset.cases] == ["agrees", "disagrees"]
    assert dataset.cases[1].input == AnswerInput(answer="no")
    assert dataset.cases[1].expected_outputs == [
        ExpectedAnswer(key="answer", equals="yes"),
        ExpectedAnswer(key="echo", equals="no"),
    ]


def test_every_invalid_case_is_reported_at_once(tmp_path: Path) -> None:
    path = write_dataset(
        tmp_path,
        FIXED_OUTPUT_EVAL.name,
        [
            {
                "case_id": "number_answer",
                "input": {"answer": 7},
                "expected_outputs": [{"key": "answer", "equals": "7"}],
            },
            {
                "case_id": "no_equals",
                "input": {"answer": "yes"},
                "expected_outputs": [{"key": "answer"}],
            },
            {
                "case_id": "stray_fields",
                "input": {"answer": "yes"},
                "note": "not a case field",
                "expected_outputs": [{"key": "answer", "equals": "yes", "weight": 2}],
            },
        ],
    )

    with pytest.raises(DatasetInvalid) as refusal:
        read_dataset(path, FIXED_OUTPUT_EVAL)

    message = str(refusal.value)
    assert "cases.0.input.answer" in message
    assert "cases.1.expected_outputs.0.equals" in message
    assert "cases.2.note" in message
    assert "cases.2.expected_outputs.0.weight" in message


def test_repeated_case_ids_are_refused(tmp_path: Path) -> None:
    path = write_dataset(
        tmp_path,
        FIXED_OUTPUT_EVAL.name,
        [build_case("twice", "answer"), build_case("once", "answer"), build_case("twice", "answer")],
    )

    message = read_refusal(path)

    assert "'twice'" in message
    assert "'once'" not in message


def test_repeated_expected_keys_in_a_case_are_refused(tmp_path: Path) -> None:
    path = write_dataset(
        tmp_path,
        FIXED_OUTPUT_EVAL.name,
        [build_case("repeats_a_key", "answer", "answer"), build_case("shares_a_key", "answer", "echo")],
    )

    message = read_refusal(path)

    assert "'repeats_a_key'" in message
    assert "'answer'" in message
    assert "shares_a_key" not in message


def test_a_case_expecting_nothing_is_refused(tmp_path: Path) -> None:
    path = write_dataset(
        tmp_path,
        FIXED_OUTPUT_EVAL.name,
        [build_case("expects_nothing"), build_case("expects_an_answer", "answer")],
    )

    message = read_refusal(path)

    assert "'expects_nothing'" in message
    assert "expects_an_answer" not in message


def test_a_dataset_written_for_another_eval_is_refused(tmp_path: Path) -> None:
    path = write_dataset(tmp_path, "another_eval", [build_case("valid_case", "answer")])

    message = read_refusal(path)

    assert "'another_eval'" in message
    assert repr(FIXED_OUTPUT_EVAL.name) in message


def test_a_dataset_without_cases_is_refused(tmp_path: Path) -> None:
    path = write_dataset(tmp_path, FIXED_OUTPUT_EVAL.name, [])

    message = read_refusal(path)

    assert str(path) in message
    assert "no cases" in message


def test_the_dataset_guard_accepts_only_the_class_parametrized_with_the_evals_models() -> None:
    assert _is_dataset_of(Dataset[AnswerInput, ExpectedAnswer], AnswerInput, ExpectedAnswer)
    assert not _is_dataset_of(Dataset, AnswerInput, ExpectedAnswer)
    assert not _is_dataset_of(Dataset[AnswerOutput, ExpectedAnswer], AnswerInput, ExpectedAnswer)


def write_dataset(tmp_path: Path, eval_name: str, cases: list[JsonValue]) -> Path:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps({"eval": eval_name, "cases": cases}), encoding="utf-8")
    return path


def build_case(case_id: str, *expected_keys: str) -> JsonValue:
    return {
        "case_id": case_id,
        "input": {"answer": "yes"},
        "expected_outputs": [{"key": key, "equals": "yes"} for key in expected_keys],
    }


def read_refusal(path: Path) -> str:
    with pytest.raises(DatasetInvalid) as refusal:
        read_dataset(path, FIXED_OUTPUT_EVAL)
    return str(refusal.value)
