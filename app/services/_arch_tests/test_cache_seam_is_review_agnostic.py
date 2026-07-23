"""Architecture: the stage-result cache seam and its sole runtime consumer
carry NO review-verdict vocabulary. A cache entry is a generic
(output row | tombstone) keyed by a fingerprint; the handler that replays those
entries applies them generically (replace-the-row, or drop it on a tombstone).
Verdict semantics — what `approve`/`modify`/`reject` mean, and the score columns
they produce — live at the web/service boundary (`app/services/review.py`,
`app/web/routers/review.py`), never in the generic cache
(`app/services/stage_cache.py`) or the handler
(`app/runtime/stages/human_review_queue.py`) that consumes it.

The rule is executable: neither the cache module nor the handler may import a
name in `_REVIEW_VOCABULARY`. Importing `RowReviewDecision` is exactly the
review-coupling this generalization removed, so its return is caught here.
"""
from __future__ import annotations

from pathlib import Path

from arch._helpers import find_imported_names, parse_module
from arch.scope import find_source_files_under

_SERVICES_DIR = Path(__file__).resolve().parents[1]
_APP_DIR = Path(__file__).resolve().parents[2]

_STAGE_CACHE = _SERVICES_DIR / "stage_cache.py"
_HANDLER = _APP_DIR / "runtime" / "stages" / "human_review_queue.py"

_REVIEW_VOCABULARY = frozenset({"RowReviewDecision"})


def find_review_vocabulary_import_offenders(paths: list[Path]) -> list[str]:
    """"<file.name>: [<name>, ...]" for every file in `paths` that imports a
    name in `_REVIEW_VOCABULARY` — a review-verdict symbol the generic cache
    seam and its handler must not know about."""
    offenders: list[str] = []
    for path in paths:
        hits = find_imported_names(parse_module(path)) & _REVIEW_VOCABULARY
        if hits:
            offenders.append(f"{path.name}: {sorted(hits)}")
    return offenders


def test_cache_seam_and_handler_import_no_review_vocabulary() -> None:
    targets = find_source_files_under(_STAGE_CACHE) + find_source_files_under(_HANDLER)
    offenders = find_review_vocabulary_import_offenders(targets)
    assert not offenders, (
        "the generic stage-result cache and the handler that replays its "
        "entries must carry no review-verdict vocabulary — verdict semantics "
        "belong at the web/service boundary (app/services/review.py), not in "
        "the cache seam:\n  " + "\n  ".join(offenders)
    )


# --- unit tests for find_review_vocabulary_import_offenders, on inline snippets ---


def test_flags_a_review_vocabulary_import(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("from app.models import RowReviewDecision\n")
    assert find_review_vocabulary_import_offenders([target]) == ["m.py: ['RowReviewDecision']"]


def test_flags_an_aliased_review_vocabulary_import(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("from app.models import RowReviewDecision as Verdict\n")
    assert find_review_vocabulary_import_offenders([target]) == ["m.py: ['RowReviewDecision']"]


def test_ignores_a_file_importing_only_neutral_names(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("from app.models import Stage\n")
    assert find_review_vocabulary_import_offenders([target]) == []
