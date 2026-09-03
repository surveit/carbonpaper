"""Architecture: a burn-down ratchet on pandas types in ``app/`` SIGNATURES.
Three rules, matching ``test_file_size_ratchet``: an unlisted module with any
pandas-typed signature is a new violation; a listed one above its entry is a
regression; a listed one at or below its entry is stale and must be lowered.
The end state is ``_OWNERS`` alone.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import ast

from arch.test_complexity_ratchet import find_app_source_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

# The modules allowed to name a pandas type in a signature at all, exempt from
# the ratchet rather than listed in it: they ARE the seam. Everything else takes
# an Arrow table and materializes a frame only by asking one of these.
_OWNERS: frozenset[str] = frozenset({"app/core/frames.py"})

# Modules that name a pandas type in a signature today, at today's count. A
# ratchet: an entry may only be LOWERED or removed, never added or raised. The
# count is per module rather than a bare name so the burn-down is measurable
# while it is in progress — a module going 20 → 8 registers instead of looking
# untouched until its last signature goes.
_ALLOWLIST: Mapping[str, int] = {
    "app/core/source_files.py": 5,
    "app/evals/dataset.py": 1,
    "app/evals/runner.py": 3,
    "app/evals/scoring.py": 3,
    "app/runtime/executor.py": 2,
    "app/runtime/lineage.py": 1,
    "app/runtime/manifest.py": 1,
    "app/runtime/preview.py": 1,
    "app/runtime/stage_output.py": 1,
    "app/runtime/stage_tests.py": 3,
    "app/runtime/stages/aggregate.py": 7,
    "app/runtime/stages/human_review_queue.py": 6,
    "app/runtime/stages/input_data.py": 1,
    "app/runtime/stages/join.py": 1,
    "app/runtime/stages/python_functions.py": 1,
    "app/services/frame_profile.py": 4,
    "app/services/run.py": 1,
    "app/services/workflow_test.py": 2,
    "app/web/eval_run_view.py": 6,
    "app/web/loading.py": 6,
    "app/web/queue_view.py": 4,
    "app/web/stage_diff.py": 9,
}

# Bare names that mean a pandas type wherever they appear in an annotation.
# `from pandas import DataFrame` is rare here but would otherwise slip the
# attribute check below.
_BARE_PANDAS_NAMES = frozenset({"DataFrame", "Series"})


def test_pandas_typed_signatures_only_shrink() -> None:
    counts = count_pandas_typed_signatures(find_app_source_files(_APP_ROOT), _REPO_ROOT)
    offenders = find_ratchet_violations(counts, _ALLOWLIST, _OWNERS)
    assert not offenders, (
        "pandas-seam ratchet — a module above `app/core/frames.py` should take an Arrow "
        "table, not a pandas type, in its signatures. A NEW entry is never the fix: route "
        "the frame through the seam, or widen `_OWNERS` (a human decision, on the record). "
        "A count that DROPPED is not a pass — lower the entry so the burn-down stays "
        "honest:\n  " + "\n  ".join(offenders)
    )


def count_pandas_typed_signatures(paths: list[Path], repo_root: Path) -> dict[str, int]:
    """Repo-relative posix path → how many of its signatures name a pandas type."""
    counts: dict[str, int] = {}
    for path in paths:
        total = _count_in_module(ast.parse(path.read_text(encoding="utf-8")))
        if total:
            counts[path.relative_to(repo_root).as_posix()] = total
    return counts


def find_ratchet_violations(
    counts: Mapping[str, int], allowlist: Mapping[str, int], owners: frozenset[str]
) -> list[str]:
    """The three rules in the module docstring, as human-readable offender lines."""
    offenders = [
        _describe_unlisted(path, count)
        for path, count in sorted(counts.items())
        if path not in allowlist and path not in owners
    ]
    offenders += [
        f"{path}: {counts[path]} pandas-typed signature(s), above its allowlist entry of "
        f"{listed} — a regression"
        for path, listed in sorted(allowlist.items())
        if counts.get(path, 0) > listed
    ]
    offenders += [
        f"{path}: down to {counts.get(path, 0)} from an allowlist entry of {listed} — lower "
        f"the entry to {counts.get(path, 0)}" + ("" if counts.get(path) else " (remove it)")
        for path, listed in sorted(allowlist.items())
        if counts.get(path, 0) < listed
    ]
    return offenders


def _describe_unlisted(path: str, count: int) -> str:
    return (
        f"{path}: {count} pandas-typed signature(s) and not on the allowlist — take the "
        f"frame as an Arrow table, or call `app.core.frames` to materialize one locally"
    )


def _count_in_module(tree: ast.Module) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_names_a_pandas_type(annotation) for annotation in _annotations_of(node))
    )


def _annotations_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    args = node.args
    annotated = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return [arg.annotation for arg in annotated if arg is not None and arg.annotation] + (
        [node.returns] if node.returns else []
    )


def _names_a_pandas_type(annotation: ast.expr) -> bool:
    """True for `pd.DataFrame`, `dict[str, pd.DataFrame]`, a bare `DataFrame`, and so on."""
    for node in ast.walk(annotation):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in ("pd", "pandas")
        ):
            return True
        if isinstance(node, ast.Name) and node.id in _BARE_PANDAS_NAMES:
            return True
    return False


# ── the three rules, over hand-built counts ──────────────────────────────────

_SEED: Mapping[str, int] = {"app/runtime/executor.py": 13}


def test_a_module_at_its_entry_is_not_an_offender() -> None:
    assert find_ratchet_violations({"app/runtime/executor.py": 13}, _SEED, frozenset()) == []


def test_an_unlisted_module_with_a_pandas_signature_is_an_offender() -> None:
    offenders = find_ratchet_violations({**_SEED, "app/web/new.py": 2}, _SEED, frozenset())
    assert len(offenders) == 1
    assert "app/web/new.py" in offenders[0] and "not on the allowlist" in offenders[0]


def test_an_owner_may_name_pandas_without_an_entry() -> None:
    counts = {**_SEED, "app/core/frames.py": 17}
    assert find_ratchet_violations(counts, _SEED, frozenset({"app/core/frames.py"})) == []


def test_a_module_above_its_entry_is_a_regression() -> None:
    offenders = find_ratchet_violations({"app/runtime/executor.py": 14}, _SEED, frozenset())
    assert "a regression" in offenders[0]


def test_a_module_below_its_entry_is_stale_and_names_the_new_number() -> None:
    offenders = find_ratchet_violations({"app/runtime/executor.py": 12}, _SEED, frozenset())
    assert "lower the entry to 12" in offenders[0]


def test_a_module_that_lost_every_pandas_signature_is_told_to_remove_the_entry() -> None:
    offenders = find_ratchet_violations({}, _SEED, frozenset())
    assert "(remove it)" in offenders[0]


def test_counting_reads_arguments_nested_generics_and_returns(tmp_path) -> None:
    source = (
        "import pandas as pd\n"
        "def a(x: pd.DataFrame) -> None: ...\n"
        "def b(x: int) -> pd.Series: ...\n"
        "def c(x: dict[str, pd.DataFrame]) -> None: ...\n"
        "def d(*, k: pd.DataFrame = None) -> None: ...\n"
        "def untouched(x: int) -> str: ...\n"
    )
    module = tmp_path / "app" / "m.py"
    module.parent.mkdir()
    module.write_text(source, encoding="utf-8")
    assert count_pandas_typed_signatures([module], tmp_path) == {"app/m.py": 4}
