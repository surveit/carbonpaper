"""A stage handle is read only by the module that owns it: `stage.function` by
app/models/stages/code.py, `stage.filter` by filter_rows.py, `stage.queue` by
human_review_queue.py. Logic about a handle elsewhere drifts from that handle's own
rules. `_GRANDFATHERED` is what the code reads TODAY, not the target, and may only
shrink — see find_python_function_warnings for the shape a move takes.
"""
from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"

# handle attribute -> the module that owns it, plus every other file that reads it
# today. Ordered so the owner reads first.
_OWNERS: dict[str, set[str]] = {
    "function": {"app/models/stages/code.py"},
    "filter": {"app/models/stages/filter_rows.py"},
    "queue": {"app/models/stages/human_review_queue.py"},
}

# Existing readers, to be worked down (🟢 #327 tracks it, including whether a type's
# runtime handler counts as a co-owner). Each entry is a file that reaches into a
# handle it does not own. THIS SET MAY ONLY SHRINK.
_GRANDFATHERED: dict[str, set[str]] = {
    "function": {
        "app/models/stages/filter_rows.py",
        "app/runtime/stages/python_functions.py",
    },
    "filter": set(),
    "queue": set(),
}


def _reads_handle(path: Path, attr: str) -> bool:
    """Deliberately syntactic — `<stage|stage_def|self>.<attr>`; a stricter check needs
    types."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    receivers = {"stage", "stage_def", "self"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == attr
                and isinstance(node.value, ast.Name) and node.value.id in receivers):
            return True
    return False


def test_a_stage_handle_is_read_only_by_the_module_that_owns_it() -> None:
    offenders: list[str] = []
    for attr, owners in _OWNERS.items():
        allowed = owners | _GRANDFATHERED[attr]
        for path in sorted(_APP.rglob("*.py")):
            rel = path.relative_to(_APP.parent).as_posix()
            if rel in allowed or "_arch_tests" in rel:
                continue
            if _reads_handle(path, attr):
                offenders.append(f"{rel} reads stage.{attr}, owned by {sorted(owners)[0]}")
    assert not offenders, (
        "a stage handle is read only by the module that owns it — move the logic into "
        "that module (see find_python_function_warnings in app/models/stages/code.py "
        "for the shape) rather than adding to _GRANDFATHERED, which may only shrink:\n  "
        + "\n  ".join(offenders)
    )


def test_the_grandfathered_list_is_honest() -> None:
    """A stale entry hides that the boundary was reached, so the list could never reach
    empty."""
    stale = [
        f"stage.{attr}: {rel} no longer reads it — drop it from _GRANDFATHERED"
        for attr, files in _GRANDFATHERED.items()
        for rel in sorted(files)
        if (_APP.parent / rel).exists() and not _reads_handle(_APP.parent / rel, attr)
    ]
    assert not stale, "\n  ".join(stale)
