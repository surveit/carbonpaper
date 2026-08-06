"""Architecture: every frame file under ``app/`` is read and written through
``app/core/frames.py``. Two operations live there and must not be confused — the
exact inverse pair for a frame WE wrote (`read_frame_file` / `write_frame_file`),
and typed ingest of a FOREIGN file against a caller-supplied dtype pin
(`read_foreign_*`). A pandas read elsewhere silently picks one of them.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

# Only `app/` is scanned. A test writes fixture files on purpose — including
# deliberately malformed ones no chokepoint would produce — so governing
# `tests/` would need an allowlist long enough to stop meaning anything. This
# matches the scope of the other AST rules over `app/`.
#
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # The chokepoint itself — where every pandas frame-IO call ends up.
        "app/core/frames.py",
        # NOT a waived violation: node_decisions.parquet genuinely should read
        # and write through the chokepoint, and cannot. `test_node_review_is_a_leaf`
        # (app/services/_arch_tests/) is a default-deny on node_review.py's
        # first-party imports allowing only `app.core.utils`, so importing
        # `app.core.frames` fails it — and node_review.py calls its own
        # load/record helpers internally, so the IO cannot move to its ten-odd
        # callers either. Extending that leaf allowlist is a human decision.
        # Harmless today: every node-decision column is a scalar, so the list-cell
        # read this rule protects has nothing to act on in that file.
        "app/services/node_review.py",
    }
)

# Each pandas/pyarrow frame-IO call, and the `app.core.frames` function that
# owns it. Keyed on the plain name, so an aliased import (`pd` vs `pandas`) and
# a bound method (`frame.to_parquet`) are both caught without tracking the alias.
_OWNED_CALLS = {
    "read_parquet": "read_frame_file (ours) / read_foreign_parquet (a user's file)",
    "read_csv": "read_frame_file (ours) / read_foreign_csv (a user's file)",
    "read_table": "read_frame_file",
    "read_json": "read_foreign_json_lines",
    "read_excel": "read_foreign_excel",
    "to_parquet": "write_frame_file / write_frame_file_with_csv_fallback",
    "to_csv": "write_frame_file / render_frame_as_csv_text",
    "write_table": "write_frame_file",
}


def find_unowned_frame_io(tree: ast.AST) -> list[tuple[int, str]]:
    """(lineno, called name) of every frame-IO call in `tree`, by whatever name it is called."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node.func)
        if name in _OWNED_CALLS:
            found.append((node.lineno, name))
    return sorted(found)


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def test_frame_files_are_read_and_written_only_through_app_core_frames() -> None:
    offenders: list[str] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [
            f"{rel}:{line}: {name}(...) — use {_OWNED_CALLS[name]}"
            for line, name in find_unowned_frame_io(tree)
        ]
    assert not offenders, (
        "a frame file is read or written outside app/core/frames.py, so the fixes that "
        "live there (a parquet read returning list cells as lists, the CSV fallback) do "
        "not apply to it:\n  " + "\n  ".join(offenders)
    )


def test_the_allowlist_names_files_that_exist() -> None:
    # Else a rename turns the rule off for a file nobody is checking any more.
    missing = [rel for rel in _ALLOWLIST if not (_REPO_ROOT / rel).is_file()]
    assert not missing, f"stale allowlist entry: {missing}"


def test_the_detector_sees_the_shapes_a_caller_actually_writes() -> None:
    # Else the rule above passes on a detector that matches nothing.
    for snippet in (
        "df = pd.read_parquet(path)\n",
        "df = pandas.read_csv(path, dtype=d)\n",
        "df = read_parquet(path)\n",
        "table = pq.read_table(path)\n",
        "frame.to_parquet(path, index=False)\n",
        "lineage.to_frame().to_csv(path, index=False)\n",
        "body = df.to_csv(index=False)\n",
    ):
        assert find_unowned_frame_io(ast.parse(snippet)), snippet
    for allowed in (
        "df = read_frame_file(path)\n",
        "write_frame_file(frame, path)\n",
        "text = render_frame_as_csv_text(frame)\n",
        "df = df.to_dict('records')\n",
    ):
        assert not find_unowned_frame_io(ast.parse(allowed)), allowed
