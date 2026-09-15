from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from evals.harness.definition import (
    EvalDefinition,
    ExpectedOutput,
    Judgement,
    LoadContext,
    Loaded,
)


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


class ExpectedAnswer(ExpectedOutput):
    equals: str


class AnswerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


def load_answer(case_input: AnswerInput, context: LoadContext) -> Loaded[AnswerOutput]:
    return Loaded(output=AnswerOutput(answer=case_input.answer), cost_usd=0.01)


def judge_answer(output: AnswerOutput, expected_outputs: list[ExpectedAnswer]) -> list[Judgement]:
    return [_judge_expected_answer(output, expected) for expected in expected_outputs]


def _judge_expected_answer(output: AnswerOutput, expected: ExpectedAnswer) -> Judgement:
    if output.answer == expected.equals:
        note = f"answer {output.answer!r} equals {expected.equals!r}"
        return Judgement(key=expected.key, outcome="matched", note=note)
    note = f"answer {output.answer!r} is not {expected.equals!r}"
    return Judgement(key=expected.key, outcome="differed", note=note)


FIXED_OUTPUT_EVAL = EvalDefinition(
    name="fixed_output",
    input_model=AnswerInput,
    expected_model=ExpectedAnswer,
    output_model=AnswerOutput,
    outcomes=("matched", "differed"),
    default_repeats=2,
    load=load_answer,
    judge=judge_answer,
)
