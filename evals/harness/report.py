from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from evals.harness.passes import (
    PassRecord,
)


class CaseNotInDataset(Exception):
    pass


def validate_pass_cases_are_in_dataset(
    record: PassRecord, dataset_case_ids: Collection[str], dataset_path: Path
) -> None:
    missing = [case_id for case_id in record.case_ids if case_id not in dataset_case_ids]
    if missing:
        listed = ", ".join(repr(case_id) for case_id in missing)
        raise CaseNotInDataset(f"{dataset_path}: the pass ran case_ids not in the dataset: {listed}")
