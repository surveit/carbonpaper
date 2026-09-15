from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path

from evals.harness.dataset import Case, read_dataset
from evals.harness.definition import (
    CaseRefused,
    EvalDefinition,
    ExpectedT,
    InputT,
    LoadContext,
    OutputT,
)
from evals.harness.passes import LoadRefused, OutputStored, PassFolder, PassRecord

_DATASET_FILE = "dataset.json"


class LoadCountNotConfirmed(Exception):
    pass


class CaseSelectionInvalid(Exception):
    pass


class CodeCommitUnreadable(Exception):
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
    dataset_path = eval_dir / _DATASET_FILE
    cases = _select_cases(read_dataset(dataset_path, definition).cases, case_ids, dataset_path)
    _validate_load_count(len(cases) * repeats, confirmed_loads)
    record = _start_pass_record(definition.name, repeats, _read_code_commit(repo_root))
    folder = PassFolder.create(eval_dir, record.pass_id)
    folder.write_record(record)
    context = LoadContext(
        pass_dir=folder.root, workspace_dir=folder.workspace_dir, repo_root=repo_root
    )
    _load_cases(definition, cases, repeats, context, folder, record)
    return folder.root


def _select_cases(
    cases: list[Case[InputT, ExpectedT]], case_ids: list[str], dataset_path: Path
) -> list[Case[InputT, ExpectedT]]:
    if not case_ids:
        return cases
    known_ids = {case.case_id for case in cases}
    unknown_ids = [case_id for case_id in case_ids if case_id not in known_ids]
    if unknown_ids:
        listed_ids = ", ".join(repr(case_id) for case_id in unknown_ids)
        raise CaseSelectionInvalid(f"{dataset_path}: case_ids not in the dataset: {listed_ids}")
    return [case for case in cases if case.case_id in case_ids]


def _validate_load_count(planned: int, confirmed_loads: int) -> None:
    if confirmed_loads != planned:
        raise LoadCountNotConfirmed(
            f"this pass plans {planned} loads; confirm with --loads {planned}"
        )


def _read_code_commit(repo_root: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8").strip()
        raise CodeCommitUnreadable(f"git rev-parse HEAD failed in {repo_root}: {stderr}")
    return result.stdout.decode("utf-8").strip()


def _start_pass_record(eval_name: str, repeats: int, code_commit: str) -> PassRecord:
    started = datetime.now()
    return PassRecord(
        eval=eval_name,
        pass_id=started.strftime("%Y%m%dT%H%M%S"),
        code_commit=code_commit,
        repeats=repeats,
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
        return LoadRefused(case_id=case.case_id, attempt=attempt, reason=refusal.reason)
    seconds = time.monotonic() - started
    folder.write_output(case.case_id, attempt, loaded.output)
    return OutputStored(
        case_id=case.case_id, attempt=attempt, seconds=seconds, cost_usd=loaded.cost_usd
    )
