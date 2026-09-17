from __future__ import annotations

import subprocess
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from evals.harness.dataset import DATASET_FILE, Case, read_dataset
from evals.harness.definition import (
    CaseRefused,
    EvalDefinition,
    ExpectedT,
    InputT,
    Judgement,
    LoadContext,
    OutputT,
)
from evals.harness.passes import (
    JudgedAttempt,
    JudgementRefused,
    LoadRefused,
    OutputStored,
    PassFolder,
    PassIncomplete as PassIncomplete,
    PassRecord,
    validate_pass_is_complete,
)
from evals.harness.report import (
    build_pass_report,
    validate_pass_cases_are_in_dataset,
    write_pass_report,
)
from evals.harness.validation import inflect


class LoadCountNotConfirmed(Exception):
    pass


class JudgementInvalid(Exception):
    pass


class CaseSelectionInvalid(Exception):
    pass


class CodeCommitUnreadable(Exception):
    pass


class RepeatsInvalid(Exception):
    pass


def run_pass(
    definition: EvalDefinition[InputT, ExpectedT, OutputT],
    *,
    eval_dir: Path,
    repeats: int,
    confirmed_loads: int,
    case_ids: list[str],
    repo_root: Path,
) -> Path:
    dataset_path = eval_dir / DATASET_FILE
    cases = _select_cases(read_dataset(dataset_path, definition).cases, case_ids, dataset_path)
    _validate_repeats(repeats)
    _validate_load_count(len(cases) * repeats, confirmed_loads)
    code_commit = _read_code_commit(repo_root)
    selected_ids = [case.case_id for case in cases]
    record = _start_pass_record(definition.name, repeats, selected_ids, code_commit)
    folder = PassFolder.create(eval_dir, record.pass_id)
    folder.write_record(record)
    context = LoadContext(
        pass_dir=folder.root, workspace_dir=folder.workspace_dir, repo_root=repo_root
    )
    _load_cases(definition, cases, repeats, context, folder, record)
    judge_pass(definition, folder.root, eval_dir=eval_dir)
    return folder.root


def judge_pass(
    definition: EvalDefinition[InputT, ExpectedT, OutputT], pass_dir: Path, *, eval_dir: Path
) -> None:
    folder = PassFolder(pass_dir)
    record = folder.read_record()
    _validate_pass_is_for_eval(record, definition.name, pass_dir)
    validate_pass_is_complete(record, pass_dir)
    dataset_path = eval_dir / DATASET_FILE
    cases_by_id = {case.case_id: case for case in read_dataset(dataset_path, definition).cases}
    validate_pass_cases_are_in_dataset(record, cases_by_id.keys(), dataset_path)
    stored = [load for load in record.loads if isinstance(load, OutputStored)]
    judged_attempts = [
        _judge_stored_output(definition, folder, cases_by_id[load.case_id], load.attempt)
        for load in stored
    ]
    folder.replace_judgements(judged_attempts)
    write_pass_report(
        build_pass_report(definition, pass_dir, eval_dir=eval_dir, against=None), pass_dir
    )


def _select_cases(
    cases: list[Case[InputT, ExpectedT]], case_ids: list[str], dataset_path: Path
) -> list[Case[InputT, ExpectedT]]:
    if not case_ids:
        return cases
    known_ids = {case.case_id for case in cases}
    unknown_ids = [case_id for case_id in case_ids if case_id not in known_ids]
    if unknown_ids:
        raise CaseSelectionInvalid(
            f"{dataset_path}: case_ids not in the dataset: {_quote_each(unknown_ids)}"
        )
    return [case for case in cases if case.case_id in case_ids]


def _validate_repeats(repeats: int) -> None:
    if repeats < 1:
        raise RepeatsInvalid(f"repeats must be at least 1, not {repeats}")


def _validate_load_count(planned: int, confirmed_loads: int) -> None:
    if confirmed_loads != planned:
        raise LoadCountNotConfirmed(
            f"this pass plans {planned} {inflect(planned, 'load')}; "
            f"confirm with --loads {planned}"
        )


def _read_code_commit(repo_root: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise CodeCommitUnreadable(f"git rev-parse HEAD failed in {repo_root}: {stderr}")
    return result.stdout.decode("utf-8").strip()


def _start_pass_record(
    eval_name: str, repeats: int, case_ids: list[str], code_commit: str
) -> PassRecord:
    started = datetime.now()
    return PassRecord(
        eval=eval_name,
        pass_id=started.strftime("%Y%m%dT%H%M%S"),
        code_commit=code_commit,
        repeats=repeats,
        case_ids=case_ids,
        started_at=started.isoformat(timespec="seconds"),
        loads=[],
    )


def _load_cases(
    definition: EvalDefinition[InputT, ExpectedT, OutputT],
    cases: list[Case[InputT, ExpectedT]],
    repeats: int,
    context: LoadContext,
    folder: PassFolder,
    record: PassRecord,
) -> None:
    for case in cases:
        for attempt in range(1, repeats + 1):
            load = _load_attempt(definition, case, attempt, context, folder)
            record.loads.append(load)
            folder.write_record(record)
            if isinstance(load, LoadRefused):
                break


def _load_attempt(
    definition: EvalDefinition[InputT, ExpectedT, OutputT],
    case: Case[InputT, ExpectedT],
    attempt: int,
    context: LoadContext,
    folder: PassFolder,
) -> OutputStored | LoadRefused:
    started = time.monotonic()
    try:
        loaded = definition.load(case.input, context)
    except CaseRefused as refusal:
        refused_after = time.monotonic() - started
        return LoadRefused(
            case_id=case.case_id, attempt=attempt, seconds=refused_after, reason=refusal.reason
        )
    seconds = time.monotonic() - started
    folder.write_output(case.case_id, attempt, loaded.output)
    return OutputStored(
        case_id=case.case_id, attempt=attempt, seconds=seconds, cost_usd=loaded.cost_usd
    )


def _validate_pass_is_for_eval(record: PassRecord, eval_name: str, pass_dir: Path) -> None:
    if record.eval != eval_name:
        raise JudgementInvalid(f"{pass_dir}: pass is for eval {record.eval!r}, not {eval_name!r}")


def _judge_stored_output(
    definition: EvalDefinition[InputT, ExpectedT, OutputT],
    folder: PassFolder,
    case: Case[InputT, ExpectedT],
    attempt: int,
) -> JudgedAttempt:
    output = folder.read_output(case.case_id, attempt, definition.output_model)
    try:
        judgements = definition.judge(output, case.expected_outputs)
    except CaseRefused as refusal:
        return JudgedAttempt(case.case_id, attempt, JudgementRefused(reason=refusal.reason))
    _validate_judgements(judgements, case, attempt, definition.outcomes)
    return JudgedAttempt(case.case_id, attempt, judgements)


def _validate_judgements(
    judgements: list[Judgement],
    case: Case[InputT, ExpectedT],
    attempt: int,
    outcomes: tuple[str, ...],
) -> None:
    expected_keys = [expected.key for expected in case.expected_outputs]
    problems = _find_key_problems(judgements, expected_keys)
    problems += _find_outcome_problems(judgements, outcomes)
    if problems:
        raise JudgementInvalid(f"case {case.case_id!r} attempt {attempt}: {'; '.join(problems)}")


def _find_key_problems(judgements: list[Judgement], expected_keys: list[str]) -> list[str]:
    counts = Counter(judgement.key for judgement in judgements)
    missing = [
        f"expected key {key!r} has no judgement" for key in expected_keys if key not in counts
    ]
    repeated = [
        f"expected key {key!r} is judged {counts[key]} times"
        for key in expected_keys
        if key in counts and counts[key] > 1
    ]
    unexpected = [
        f"key {key!r} is not an expected key of the case"
        for key in counts
        if key not in expected_keys
    ]
    return missing + repeated + unexpected


def _find_outcome_problems(judgements: list[Judgement], outcomes: tuple[str, ...]) -> list[str]:
    allowed = _quote_each(outcomes)
    return [
        f"key {judgement.key!r} has outcome {judgement.outcome!r}, not one of {allowed}"
        for judgement in judgements
        if judgement.outcome not in outcomes
    ]


def _quote_each(values: Iterable[str]) -> str:
    return ", ".join(repr(value) for value in values)
