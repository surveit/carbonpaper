"""Which modules under ``app/`` may import pandas, and why. Arrow is the wire
format; pandas is a local tool for work that is pandas-shaped, never something
that travels between stages. An unlisted importer fails, and a listed one that
has stopped importing pandas fails as stale.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import ast

from arch.test_complexity_ratchet import find_app_source_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

# Module → the pandas-shaped work it does. A reason is not decoration: it is what
# a reviewer checks the import against, and what makes a stale entry obvious.
_MAY_IMPORT_PANDAS: Mapping[str, str] = {
    # ── the seam itself ──────────────────────────────────────────────────────
    "app/core/frames.py": "owns the arrow/pandas conversion both directions",
    # ── stage handlers: the authored code or vectorised work IS pandas ───────
    "app/runtime/stages/python_functions.py": "hands the frame to an authored transform(df)",
    "app/runtime/stages/aggregate.py": "group-by and the aggregate formulas",
    "app/runtime/stages/join.py": "pd.merge, including its m:1 validation",
    "app/runtime/stages/human_review_queue.py": "queue stats and the pending-review scan",
    "app/runtime/stages/input_data.py": "reads csv/xlsx/json-lines, which carry no types",
    # ── files that hold characters rather than types ─────────────────────────
    "app/core/source_files.py": "profiles an uploaded csv/xlsx before a schema exists",
    "app/core/row_search.py": "searches cells of a frame a reviewer is looking at",
    # ── the run's own records, which are frames on disk ──────────────────────
    "app/runtime/lineage.py": "row lineage is stored as a frame sidecar",
    "app/runtime/manifest.py": "reads a stage output back for a reader",
    "app/runtime/executor.py": "injected outputs arrive as frames from a caller above",
    "app/runtime/runner.py": "catches pandas' own parser error when a source will not read",
    "app/runtime/citations.py": "compares a published cell to its source cell",
    "app/runtime/stage_output.py": "`from_frame` is how a pandas-shaped handler returns",
    "app/runtime/preview.py": "hands a frame to the page that renders it",
    "app/runtime/stage_tests.py": "compares an authored expected table to what ran",
    # ── services and evals: the boundary to something that wants a frame ─────
    "app/services/frame_profile.py": "profiles a frame for the schema editor",
    "app/services/run.py": "passes injected frames through to the executor",
    "app/services/workflow_test.py": "builds the frames an authored test declares",
    "app/evals/dataset.py": "reads an eval table off disk",
    "app/evals/runner.py": "scores against an authored expected frame",
    "app/evals/scoring.py": "the comparison itself is elementwise over frames",
    # ── presentation: every cell is formatted as text ────────────────────────
    "app/web/loading.py": "renders a stage output as an HTML table",
    "app/web/stage_diff.py": "aligns two frames column by column for the diff",
    "app/web/queue_view.py": "renders the review queue",
    "app/web/eval_run_view.py": "renders an eval run's scored rows",
}


def test_only_declared_modules_import_pandas() -> None:
    importers = find_pandas_importers(find_app_source_files(_APP_ROOT), _REPO_ROOT)
    offenders = find_import_violations(importers, _MAY_IMPORT_PANDAS)
    assert not offenders, (
        "pandas is a local tool, not a wire format — a module that imports it must say what "
        "pandas-shaped work it does. Take the arrow table instead, or add an entry to "
        "`_MAY_IMPORT_PANDAS` with a reason a reviewer can check:\n  " + "\n  ".join(offenders)
    )


def find_pandas_importers(paths: list[Path], repo_root: Path) -> set[str]:
    return {
        path.relative_to(repo_root).as_posix()
        for path in paths
        if _imports_pandas(ast.parse(path.read_text(encoding="utf-8")))
    }


def find_import_violations(importers: set[str], allowed: Mapping[str, str]) -> list[str]:
    undeclared = [
        f"{path}: imports pandas and is not listed" for path in sorted(importers - set(allowed))
    ]
    stale = [
        f"{path}: listed as {allowed[path]!r} but no longer imports pandas — remove the entry"
        for path in sorted(set(allowed) - importers)
    ]
    return undeclared + stale


def _imports_pandas(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "pandas" or alias.name.startswith("pandas.")
                   for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pandas":
            return True
    return False


# ── the two rules, over hand-built inputs ────────────────────────────────────

_SEED: Mapping[str, str] = {"app/core/frames.py": "owns the conversion"}


def test_a_listed_importer_is_not_an_offender() -> None:
    assert find_import_violations({"app/core/frames.py"}, _SEED) == []


def test_an_unlisted_importer_is_an_offender() -> None:
    offenders = find_import_violations({"app/core/frames.py", "app/web/new.py"}, _SEED)
    assert len(offenders) == 1 and "app/web/new.py" in offenders[0]


def test_a_listed_module_that_stopped_importing_pandas_is_stale() -> None:
    offenders = find_import_violations(set(), _SEED)
    assert len(offenders) == 1 and "remove the entry" in offenders[0]


def test_both_import_forms_are_seen(tmp_path) -> None:
    for source in ("import pandas as pd\n", "from pandas import DataFrame\n",
                   "import pandas.api.types\n"):
        assert _imports_pandas(ast.parse(source))
    assert not _imports_pandas(ast.parse("import pyarrow as pa\n"))
