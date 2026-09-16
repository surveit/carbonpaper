from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from evals.harness.definition import (
    CaseRefused,
    EvalDefinition,
    ExpectedT,
    InputT,
    Judgement,
    OutputT,
)
from evals.harness.validation import (
    describe_errors,
    find_repeated,
    join_problems,
    validate_as_json,
)

RULINGS_FILE = "rulings.json"


class RulingOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    outcome: str


@dataclass(frozen=True)
class Ruling(Generic[OutputT, ExpectedT]):
    ruling_id: str
    output: OutputT
    expected_outputs: list[ExpectedT]
    outcomes: list[RulingOutcome]


class Disagreement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruling_id: str
    key: str
    person: str
    judge: str
    note: str


class RulingsInvalid(Exception):
    pass


class RulingNotJudged(Exception):
    pass


def find_ruling_disagreements(
    definition: EvalDefinition[InputT, ExpectedT, OutputT], rulings_path: Path
) -> list[Disagreement]:
    return [
        disagreement
        for ruling in _read_rulings(rulings_path, definition)
        for disagreement in _find_disagreements(definition, ruling, rulings_path)
    ]


class _RawRuling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruling_id: str
    output: JsonValue
    expected_outputs: list[JsonValue]
    outcomes: list[RulingOutcome]


def _read_rulings(
    path: Path, definition: EvalDefinition[InputT, ExpectedT, OutputT]
) -> list[Ruling[OutputT, ExpectedT]]:
    raw_values = _parse_raw_rulings(path)
    if not raw_values:
        raise RulingsInvalid(join_problems(path, ["no rulings to compare"]))
    _validate_ruling_ids_are_distinct(raw_values, path)
    rulings: list[Ruling[OutputT, ExpectedT]] = []
    problems: list[str] = []
    for position, value in enumerate(raw_values):
        ruling_or_problems = _read_ruling(position, value, definition)
        if isinstance(ruling_or_problems, Ruling):
            rulings.append(ruling_or_problems)
        else:
            problems += ruling_or_problems
    if problems:
        raise RulingsInvalid(join_problems(path, problems))
    return rulings


def _parse_raw_rulings(path: Path) -> list[JsonValue]:
    try:
        return _RAW_RULINGS.validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise RulingsInvalid(join_problems(path, describe_errors("rulings", (), error))) from error


def _validate_ruling_ids_are_distinct(raw_values: list[JsonValue], path: Path) -> None:
    readable_ids = [_find_readable_ruling_id(value) for value in raw_values]
    repeated_ids = find_repeated(ruling_id for ruling_id in readable_ids if ruling_id is not None)
    if repeated_ids:
        problems = [f"ruling_id {ruling_id!r} is repeated" for ruling_id in repeated_ids]
        raise RulingsInvalid(join_problems(path, problems))


def _read_ruling(
    position: int, value: JsonValue, definition: EvalDefinition[InputT, ExpectedT, OutputT]
) -> Ruling[OutputT, ExpectedT] | list[str]:
    subject = _name_ruling(position, value)
    try:
        raw = validate_as_json(_RawRuling, value)
    except ValidationError as error:
        return describe_errors(subject, (), error)
    expected_outputs, problems = _read_expected_outputs(raw, definition.expected_model, subject)
    problems += _find_outcome_problems(raw.outcomes, definition.outcomes, subject)
    if not problems:
        problems += _find_ruled_key_problems(raw.outcomes, expected_outputs, subject)
    try:
        output = validate_as_json(definition.output_model, raw.output)
    except ValidationError as error:
        return describe_errors(subject, ("output",), error) + problems
    if problems:
        return problems
    return Ruling(
        ruling_id=raw.ruling_id,
        output=output,
        expected_outputs=expected_outputs,
        outcomes=raw.outcomes,
    )


def _read_expected_outputs(
    raw: _RawRuling, expected_model: type[ExpectedT], subject: str
) -> tuple[list[ExpectedT], list[str]]:
    expected_outputs: list[ExpectedT] = []
    problems: list[str] = []
    for position, value in enumerate(raw.expected_outputs):
        try:
            expected_outputs.append(validate_as_json(expected_model, value))
        except ValidationError as error:
            problems += describe_errors(subject, ("expected_outputs", position), error)
    if not raw.expected_outputs:
        problems.append(f"{subject} has no expected outputs")
    repeated_keys = find_repeated(expected.key for expected in expected_outputs)
    problems += [f"{subject} lists expected key {key!r} more than once" for key in repeated_keys]
    return expected_outputs, problems


def _find_outcome_problems(
    outcomes: list[RulingOutcome], allowed_outcomes: tuple[str, ...], subject: str
) -> list[str]:
    allowed = ", ".join(repr(outcome) for outcome in allowed_outcomes)
    return [
        f"{subject} rules outcome {outcome.outcome!r} for key {outcome.key!r}, not one of {allowed}"
        for outcome in outcomes
        if outcome.outcome not in allowed_outcomes
    ]


def _find_ruled_key_problems(
    outcomes: list[RulingOutcome], expected_outputs: list[ExpectedT], subject: str
) -> list[str]:
    ruled_keys = [outcome.key for outcome in outcomes]
    expected_keys = [expected.key for expected in expected_outputs]
    unexpected = [
        f"rules on key {key!r}, which it does not expect"
        for key in ruled_keys
        if key not in expected_keys
    ]
    unruled = [
        f"expects key {key!r} but rules on no outcome for it"
        for key in expected_keys
        if key not in ruled_keys
    ]
    repeated = [f"rules on key {key!r} more than once" for key in find_repeated(ruled_keys)]
    return [f"{subject} {problem}" for problem in unexpected + unruled + repeated]


def _name_ruling(position: int, value: JsonValue) -> str:
    ruling_id = _find_readable_ruling_id(value)
    return f"rulings[{position}]" if ruling_id is None else f"ruling {ruling_id!r}"


def _find_readable_ruling_id(value: JsonValue) -> str | None:
    if not isinstance(value, dict):
        return None
    ruling_id = value.get("ruling_id")
    return ruling_id if isinstance(ruling_id, str) else None


def _find_disagreements(
    definition: EvalDefinition[InputT, ExpectedT, OutputT],
    ruling: Ruling[OutputT, ExpectedT],
    rulings_path: Path,
) -> list[Disagreement]:
    judgements = _judge_ruling(definition, ruling, rulings_path)
    _validate_judgements_match_the_ruled_keys(judgements, ruling, rulings_path)
    judged = {judgement.key: judgement for judgement in judgements}
    return [
        Disagreement(
            ruling_id=ruling.ruling_id,
            key=outcome.key,
            person=outcome.outcome,
            judge=judged[outcome.key].outcome,
            note=judged[outcome.key].note,
        )
        for outcome in ruling.outcomes
        if judged[outcome.key].outcome != outcome.outcome
    ]


def _judge_ruling(
    definition: EvalDefinition[InputT, ExpectedT, OutputT],
    ruling: Ruling[OutputT, ExpectedT],
    rulings_path: Path,
) -> list[Judgement]:
    try:
        return definition.judge(ruling.output, ruling.expected_outputs)
    except CaseRefused as refusal:
        problem = f"ruling {ruling.ruling_id!r}: the judge refused: {refusal.reason}"
        raise RulingNotJudged(join_problems(rulings_path, [problem])) from refusal


def _validate_judgements_match_the_ruled_keys(
    judgements: list[Judgement], ruling: Ruling[OutputT, ExpectedT], rulings_path: Path
) -> None:
    problems = _find_judging_problems(judgements, ruling)
    if problems:
        raise RulingNotJudged(join_problems(rulings_path, problems))


def _find_judging_problems(
    judgements: list[Judgement], ruling: Ruling[OutputT, ExpectedT]
) -> list[str]:
    ruled_keys = [outcome.key for outcome in ruling.outcomes]
    judged_keys = [judgement.key for judgement in judgements]
    unjudged = [
        f"the judge gave no outcome for key {key!r}"
        for key in ruled_keys
        if key not in judged_keys
    ]
    unruled = [
        f"the judge judged key {key!r}, which the ruling does not rule on"
        for key in judged_keys
        if key not in ruled_keys
    ]
    repeated = [f"the judge judged key {key!r} more than once" for key in find_repeated(judged_keys)]
    return [f"ruling {ruling.ruling_id!r}: {problem}" for problem in unjudged + unruled + repeated]


_RAW_RULINGS: TypeAdapter[list[JsonValue]] = TypeAdapter(list[JsonValue])
