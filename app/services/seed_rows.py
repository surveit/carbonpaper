"""Seeds artifact — record/load user-named corpus rows and grade them.

Storage is one parquet ledger `<project>/seeds.parquet`, columns = the SeedRow
fields (mirrors app.services.node_review's parquet-ledger pattern). This module
is pure stdlib + pandas: it imports nothing from app.runtime, so the run seam
(services never drives the runner) holds — callers pass the pipeline's flagged
keys in as data.

Row hashing uses a local `hash_row_content` rather than node_review's
`node_content_hash`: that helper strips stage-spec bookkeeping keys
(`_filename`/`_order`/`_error`) which have no meaning for an arbitrary corpus
row, so a same-name column would be silently dropped from the hash. Same sha1[:16]
canonical-JSON convention, without the stage-specific strip.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
from pandas import DataFrame

from app.core.errors import SeedRowNotFoundError
from app.models.seed_rows import SeedOutcome, SeedRow

# Columns of the seeds ledger, in order — the SeedRow fields.
SEED_COLUMNS: list[str] = ["row_key", "outcome", "note", "row_content_hash"]


def record_seeds(
    project_dir: Path, seeds: list[SeedRow], corpus: DataFrame, key_column: str
) -> None:
    """Persist `seeds` to `<project>/seeds.parquet`, stamping each seed's
    `row_content_hash` from the live corpus row it names.

    Every seed's `row_key` must resolve to a row in `corpus[key_column]`; a miss
    raises SeedRowNotFoundError naming the key rather than dropping the seed."""
    rows = []
    for seed in seeds:
        content = _find_row_content(corpus, key_column, seed.row_key)
        if content is None:
            raise SeedRowNotFoundError(
                f"seed row_key {seed.row_key!r} not found in corpus column {key_column!r}"
            )
        rows.append(
            {
                "row_key": seed.row_key,
                "outcome": seed.outcome.value,
                "note": seed.note,
                "row_content_hash": hash_row_content(content),
            }
        )
    frame = pd.DataFrame(rows, columns=SEED_COLUMNS)
    frame.to_parquet(seeds_path(project_dir), index=False)


def load_seeds(project_dir: Path) -> list[SeedRow]:
    """Load the seeds ledger, or `[]` when a project has none yet."""
    path = seeds_path(project_dir)
    if not path.exists():
        return []
    frame = pd.read_parquet(path)
    return [
        SeedRow(
            row_key=str(row["row_key"]),
            outcome=SeedOutcome(row["outcome"]),
            note=None if pd.isna(row["note"]) else str(row["note"]),
            row_content_hash=str(row["row_content_hash"]),
        )
        for _, row in frame.iterrows()
    ]


def find_stale_seeds(
    seeds: list[SeedRow], corpus: DataFrame, key_column: str
) -> list[str]:
    """One message per seed whose corpus row has drifted from the recording: the
    row is gone from `corpus[key_column]`, or its content hash no longer matches.
    `[]` when every seed still matches its recorded row."""
    messages: list[str] = []
    for seed in seeds:
        content = _find_row_content(corpus, key_column, seed.row_key)
        if content is None:
            messages.append(f"seed {seed.row_key} is stale: no longer in the corpus")
        elif hash_row_content(content) != seed.row_content_hash:
            messages.append(
                f"seed {seed.row_key} is stale: corpus row content changed"
            )
    return messages


def find_failing_seeds(
    seeds: list[SeedRow], positive_keys: set[str], stale_messages: list[str]
) -> list[str]:
    """One message per unmet expectation, `find_failing_stage_tests` style; `[]`
    when every seed's outcome holds and nothing is stale.

    `positive_keys` are the corpus keys the pipeline flagged. A must-catch seed
    absent from them, or a must-not-catch seed present in them, is a failure.
    Every `stale_messages` entry is included verbatim — stale seeds are failing,
    never skipped."""
    failures: list[str] = list(stale_messages)
    for seed in seeds:
        if seed.outcome == SeedOutcome.MUST_CATCH and seed.row_key not in positive_keys:
            failures.append(f"must-catch seed {seed.row_key} was not flagged")
        elif seed.outcome == SeedOutcome.MUST_NOT_CATCH and seed.row_key in positive_keys:
            failures.append(f"must-not-catch seed {seed.row_key} was flagged")
    return failures


def seeds_path(project_dir: Path) -> Path:
    """`<project>/seeds.parquet` — the single seeds ledger for a project."""
    return Path(project_dir) / "seeds.parquet"


def hash_row_content(row: dict[str, object]) -> str:
    """Stable sha1 (first 16 hex chars) of a corpus row's canonical content.

    json.dumps with sort_keys makes column order irrelevant, tight separators
    drop incidental whitespace, and default=str serializes non-JSON scalars
    (dates, numpy types). Same convention as node_review.node_content_hash."""
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _find_row_content(
    corpus: DataFrame, key_column: str, row_key: str
) -> dict[str, object] | None:
    """The first corpus row whose `key_column` equals `row_key`, as a dict, or
    None when no row matches. String-compares the key so numeric key columns
    match a string `row_key`."""
    matches = corpus[corpus[key_column].astype(str) == row_key]
    if matches.empty:
        return None
    return {str(k): v for k, v in matches.iloc[0].to_dict().items()}
