from __future__ import annotations

import html
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from evals.harness.dataset import DATASET_FILE, Case, read_dataset
from evals.harness.definition import EvalDefinition, ExpectedT, InputT, Judgement, OutputT
from evals.harness.validation import inflect
from evals.harness.passes import (
    JudgedAttempt,
    JudgementFile,
    JudgementRefused,
    LoadRecord,
    LoadRefused,
    OutputStored,
    PassFolder,
    PassRecord,
    validate_pass_is_complete,
)


class OutcomeCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str
    count: int


class AttemptOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int
    outcome: str


class ExpectedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    outcome_by_attempt: list[AttemptOutcome]
    counts: list[OutcomeCount]
    judged: int
    latest_note: str | None
    earlier_counts: list[OutcomeCount] | None


class CaseSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    rows: list[ExpectedRow]
    refusals: list[str]


class PassReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval: str
    pass_id: str
    code_commit: str
    repeats: int
    loads_started: int
    loads_refused: int
    seconds: float
    cost_usd: float | None
    loads_without_cost: int
    against_pass_id: str | None
    cases: list[CaseSection]


class JudgementMissing(Exception):
    pass


class CaseNotInDataset(Exception):
    pass


class EarlierPassInvalid(Exception):
    pass


class JudgementOutcomeUnknown(Exception):
    pass


def build_pass_report(
    definition: EvalDefinition[InputT, ExpectedT, OutputT],
    pass_dir: Path,
    *,
    eval_dir: Path,
    against: Path | None,
) -> PassReport:
    folder = PassFolder(pass_dir)
    record = folder.read_record()
    validate_pass_is_complete(record, pass_dir)
    judged_attempts = _read_judged_attempts(folder, record, pass_dir)
    _validate_judged_outcomes_are_known(judged_attempts, definition.outcomes, pass_dir)
    earlier = None if against is None else _read_earlier_pass(against, record, definition.outcomes)
    stored = [load for load in record.loads if isinstance(load, OutputStored)]
    return PassReport(
        eval=record.eval,
        pass_id=record.pass_id,
        code_commit=record.code_commit,
        repeats=record.repeats,
        loads_started=len(record.loads),
        loads_refused=sum(1 for load in record.loads if isinstance(load, LoadRefused)),
        seconds=sum(load.seconds for load in record.loads),
        cost_usd=_find_pass_cost(stored),
        loads_without_cost=sum(1 for load in stored if load.cost_usd is None),
        against_pass_id=None if earlier is None else earlier.pass_id,
        cases=_build_case_sections(definition, eval_dir, record, judged_attempts, earlier),
    )


def write_pass_report(report: PassReport, pass_dir: Path) -> None:
    (pass_dir / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (pass_dir / "report.html").write_text(_render_report_page(report), encoding="utf-8")


def validate_pass_cases_are_in_dataset(
    record: PassRecord, dataset_case_ids: Collection[str], dataset_path: Path
) -> None:
    missing = [case_id for case_id in record.case_ids if case_id not in dataset_case_ids]
    if missing:
        listed = ", ".join(repr(case_id) for case_id in missing)
        raise CaseNotInDataset(f"{dataset_path}: the pass ran case_ids not in the dataset: {listed}")


@dataclass(frozen=True)
class _EarlierPass:
    pass_id: str
    counts_by_case_and_key: dict[tuple[str, str], list[OutcomeCount]]


def _read_judged_attempts(
    folder: PassFolder, record: PassRecord, pass_dir: Path
) -> list[JudgedAttempt]:
    found = [
        (load, folder.find_judgement(load.case_id, load.attempt))
        for load in record.loads
        if isinstance(load, OutputStored)
    ]
    _validate_every_stored_output_is_judged(found, pass_dir)
    return [
        JudgedAttempt(case_id=load.case_id, attempt=load.attempt, judgements=judgements)
        for load, judgements in found
        if judgements is not None
    ]


def _validate_every_stored_output_is_judged(
    found: list[tuple[OutputStored, JudgementFile | None]], pass_dir: Path
) -> None:
    unjudged = [load for load, judgements in found if judgements is None]
    if unjudged:
        listed = "; ".join(f"case {load.case_id!r} attempt {load.attempt}" for load in unjudged)
        raise JudgementMissing(f"{pass_dir}: no judgement for {listed}")


def _validate_judged_outcomes_are_known(
    judged_attempts: list[JudgedAttempt], outcomes: tuple[str, ...], pass_dir: Path
) -> None:
    allowed = ", ".join(repr(outcome) for outcome in outcomes)
    unknown = [
        f"case {judged.case_id!r} attempt {judged.attempt} key {judgement.key!r} "
        f"has outcome {judgement.outcome!r}, not one of {allowed}"
        for judged in judged_attempts
        if isinstance(judged.judgements, list)
        for judgement in judged.judgements
        if judgement.outcome not in outcomes
    ]
    if unknown:
        raise JudgementOutcomeUnknown(f"{pass_dir}: {'; '.join(unknown)}")


def _find_pass_cost(stored: list[OutputStored]) -> float | None:
    if not stored:
        return None
    reported = [load.cost_usd for load in stored if load.cost_usd is not None]
    return sum(reported) if len(reported) == len(stored) else None


def _build_case_sections(
    definition: EvalDefinition[InputT, ExpectedT, OutputT],
    eval_dir: Path,
    record: PassRecord,
    judged_attempts: list[JudgedAttempt],
    earlier: _EarlierPass | None,
) -> list[CaseSection]:
    dataset_path = eval_dir / DATASET_FILE
    cases_by_id = {case.case_id: case for case in read_dataset(dataset_path, definition).cases}
    validate_pass_cases_are_in_dataset(record, cases_by_id.keys(), dataset_path)
    return [
        _build_case_section(
            cases_by_id[case_id],
            [load for load in record.loads if load.case_id == case_id],
            [judged for judged in judged_attempts if judged.case_id == case_id],
            definition.outcomes,
            earlier,
        )
        for case_id in record.case_ids
    ]


def _build_case_section(
    case: Case[InputT, ExpectedT],
    loads: list[LoadRecord],
    judged_attempts: list[JudgedAttempt],
    outcomes: tuple[str, ...],
    earlier: _EarlierPass | None,
) -> CaseSection:
    return CaseSection(
        case_id=case.case_id,
        rows=[
            _build_expected_row(case.case_id, expected.key, judged_attempts, outcomes, earlier)
            for expected in case.expected_outputs
        ],
        refusals=_describe_refusals(loads, judged_attempts),
    )


def _build_expected_row(
    case_id: str,
    key: str,
    judged_attempts: list[JudgedAttempt],
    outcomes: tuple[str, ...],
    earlier: _EarlierPass | None,
) -> ExpectedRow:
    judgements = _find_judgements_by_attempt(judged_attempts, key)
    return ExpectedRow(
        key=key,
        outcome_by_attempt=[
            AttemptOutcome(attempt=attempt, outcome=judgements[attempt].outcome)
            for attempt in sorted(judgements)
        ],
        counts=_count_outcomes(outcomes, judgements),
        judged=len(judgements),
        latest_note=_find_latest_note(judgements),
        earlier_counts=_find_earlier_counts(earlier, case_id, key),
    )


def _find_judgements_by_attempt(
    judged_attempts: list[JudgedAttempt], key: str
) -> dict[int, Judgement]:
    return {
        judged.attempt: judgement
        for judged in judged_attempts
        if isinstance(judged.judgements, list)
        for judgement in judged.judgements
        if judgement.key == key
    }


def _count_outcomes(
    outcomes: tuple[str, ...], judgements: dict[int, Judgement]
) -> list[OutcomeCount]:
    judged = [judgement.outcome for judgement in judgements.values()]
    return [OutcomeCount(outcome=outcome, count=judged.count(outcome)) for outcome in outcomes]


def _find_latest_note(judgements: dict[int, Judgement]) -> str | None:
    latest = max(judgements, default=None)
    return None if latest is None else judgements[latest].note


def _find_earlier_counts(
    earlier: _EarlierPass | None, case_id: str, key: str
) -> list[OutcomeCount] | None:
    if earlier is None:
        return None
    return earlier.counts_by_case_and_key.get((case_id, key))


def _describe_refusals(loads: list[LoadRecord], judged_attempts: list[JudgedAttempt]) -> list[str]:
    reasons = _find_refused_judgement_reasons(judged_attempts)
    described: list[str] = []
    for load in sorted(loads, key=lambda load: load.attempt):
        if isinstance(load, LoadRefused):
            described.append(f"attempt {load.attempt} load refused: {load.reason}")
        elif load.attempt in reasons:
            described.append(f"attempt {load.attempt} judgement refused: {reasons[load.attempt]}")
    return described


def _find_refused_judgement_reasons(judged_attempts: list[JudgedAttempt]) -> dict[int, str]:
    return {
        judged.attempt: judged.judgements.reason
        for judged in judged_attempts
        if isinstance(judged.judgements, JudgementRefused)
    }


def _read_earlier_pass(
    against: Path, record: PassRecord, outcomes: tuple[str, ...]
) -> _EarlierPass:
    folder = PassFolder(against)
    earlier = folder.read_record()
    _validate_earlier_pass_is_for_the_same_eval(earlier, record.eval, against)
    validate_pass_is_complete(earlier, against)
    judged_attempts = _read_judged_attempts(folder, earlier, against)
    _validate_judged_outcomes_are_known(judged_attempts, outcomes, against)
    return _EarlierPass(
        pass_id=earlier.pass_id,
        counts_by_case_and_key=_count_outcomes_by_case_and_key(judged_attempts, outcomes),
    )


def _validate_earlier_pass_is_for_the_same_eval(
    earlier: PassRecord, eval_name: str, against: Path
) -> None:
    if earlier.eval != eval_name:
        raise EarlierPassInvalid(f"{against}: pass is for eval {earlier.eval!r}, not {eval_name!r}")


def _count_outcomes_by_case_and_key(
    judged_attempts: list[JudgedAttempt], outcomes: tuple[str, ...]
) -> dict[tuple[str, str], list[OutcomeCount]]:
    counts: dict[tuple[str, str], list[OutcomeCount]] = {}
    for case_id, key in _find_judged_keys(judged_attempts):
        for_case = [judged for judged in judged_attempts if judged.case_id == case_id]
        counts[case_id, key] = _count_outcomes(outcomes, _find_judgements_by_attempt(for_case, key))
    return counts


def _find_judged_keys(judged_attempts: list[JudgedAttempt]) -> list[tuple[str, str]]:
    return list(
        dict.fromkeys(
            (judged.case_id, judgement.key)
            for judged in judged_attempts
            if isinstance(judged.judgements, list)
            for judgement in judged.judgements
        )
    )


def _render_report_page(report: PassReport) -> str:
    title = html.escape(f"{report.eval} pass {report.pass_id}")
    sections = [
        _render_header(report),
        *(_render_case(case, report.against_pass_id is not None) for case in report.cases),
    ]
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{title}</title>\n<style>\n{_STYLE}</style>\n</head>\n"
        f"<body>\n<h1>{title}</h1>\n" + "\n".join(sections) + "\n</body>\n</html>\n"
    )


def _render_header(report: PassReport) -> str:
    facts = [
        f"eval: {report.eval}",
        f"pass: {report.pass_id}",
        f"code commit: {report.code_commit}",
        f"repeats: {report.repeats}",
        f"loads started: {report.loads_started}",
        f"loads refused: {report.loads_refused}",
        f"seconds: {report.seconds:.3f}",
        _describe_cost(report),
    ]
    if report.against_pass_id is not None:
        facts.append(f"beside pass: {report.against_pass_id}")
    return _render_list("facts", facts)


def _describe_cost(report: PassReport) -> str:
    if report.cost_usd is None:
        loads = inflect(report.loads_without_cost, "load")
        return f"cost not reported for {report.loads_without_cost} {loads}"
    return f"cost ${report.cost_usd:.6f}"


def _render_case(case: CaseSection, shows_earlier: bool) -> str:
    attempts = sorted({entry.attempt for row in case.rows for entry in row.outcome_by_attempt})
    rows = "".join(_render_row(row, attempts, shows_earlier) for row in case.rows)
    return (
        f"<h2>{html.escape(case.case_id)}</h2>\n<table>\n"
        f"{_render_headers(attempts, shows_earlier)}{rows}</table>\n"
        f"{_render_refusals(case.refusals)}"
    )


def _render_headers(attempts: list[int], shows_earlier: bool) -> str:
    headers = ["expected key", *(f"attempt {attempt}" for attempt in attempts), "counts"]
    if shows_earlier:
        headers.append("earlier counts")
    headers.append("latest note")
    return "<tr>" + "".join(f"<th>{html.escape(header)}</th>" for header in headers) + "</tr>\n"


def _render_row(row: ExpectedRow, attempts: list[int], shows_earlier: bool) -> str:
    outcomes = {entry.attempt: entry.outcome for entry in row.outcome_by_attempt}
    cells = [
        row.key,
        *(_describe_cell(outcomes.get(attempt)) for attempt in attempts),
        _describe_counts(row.counts),
    ]
    if shows_earlier:
        cells.append(_ABSENT if row.earlier_counts is None else _describe_counts(row.earlier_counts))
    cells.append(_describe_cell(row.latest_note))
    return "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in cells) + "</tr>\n"


def _describe_counts(counts: list[OutcomeCount]) -> str:
    return ", ".join(f"{count.outcome} {count.count}" for count in counts)


def _describe_cell(value: str | None) -> str:
    return _ABSENT if value is None else value


def _render_refusals(refusals: list[str]) -> str:
    return "" if not refusals else _render_list("refusals", refusals) + "\n"


def _render_list(css_class: str, items: list[str]) -> str:
    rendered = "".join(f"<li>{html.escape(item)}</li>\n" for item in items)
    return f'<ul class="{html.escape(css_class)}">\n{rendered}</ul>'


_ABSENT = "—"
_STYLE = (
    "body{font-family:system-ui,sans-serif;margin:2rem;color:#222}\n"
    "ul.facts,ul.refusals{list-style:none;padding:0}\n"
    "table{border-collapse:collapse;margin:0 0 1rem}\n"
    "th,td{border:1px solid #bbb;padding:0.25rem 0.5rem;text-align:left;vertical-align:top}\n"
    "th{background:#f2f2f2}\n"
)
