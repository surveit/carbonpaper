"""Architecture: a project's state lives in the document store or in a frame.
Nothing under ``app/`` writes a file except the kinds that are not state — a
frame, an export the user asked for, a file the user handed us. The rule the
schemas/, compiled/, document.md, manifest.json, events.jsonl and
fingerprint-sidecar migrations were each an instance of.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

# Only `app/` is scanned: a test writes fixture files on purpose, so governing
# `tests/` would need an allowlist long enough to stop meaning anything.
#
# Each entry says WHY that file may write, because the reason is the rule — a new
# entry is only legitimate if it names one of these four kinds, and "some state I
# needed to keep" is not one of them.
_ALLOWED_WRITERS: dict[str, str] = {
    # 1. Frames. The sanctioned on-disk format, and its one chokepoint.
    "app/core/frames.py": "the frame store — parquet is the other half of the rule",

    # 2. An export the user asked us to build: a review packet, a project bundle.
    # These leave the system; they are not read back as state.
    "app/services/review_packet/data.py": "builds a downloadable review packet",
    "app/services/review_packet/checksums.py": "builds a downloadable review packet",
    "app/web/review_packet/pages.py": "builds a downloadable review packet",
    "app/web/review_packet/lineage.py": "builds a downloadable review packet",

    # 2b. The artifact a report stage publishes — sandboxed code reaches disk only here.
    "app/runtime/artifacts.py": "writes the files a report stage publishes",

    # 3. A file the user handed us. Raw bytes we did not author and must not
    # reinterpret — an input CSV, an eval dataset.
    "app/core/files.py": "stages and content-addresses an uploaded data file",
    "app/evals/store.py": "receives an uploaded eval dataset",

    # Deleting a project's working-copy DIRECTORY, which still holds its frames
    # and uploads. Its documents are a separate act.
    "app/services/project.py": "removes a deleted project's directory",
}

# The calls that put BYTES on disk, plus the two that take them away. `mkdir` is
# deliberately absent: a directory holds no state, and the run dir is still made
# to hold frames. `copy`/`copy2` are absent too — the bare attribute name is
# `df.copy()` far more often than it is shutil's.
_WRITE_CALLS = frozenset({
    "write_text", "write_bytes", "copyfileobj", "copytree", "unlink", "rmtree",
})
_WRITING_MODES = frozenset("wax+")


def find_file_writes(tree: ast.AST) -> list[tuple[int, str]]:
    """(lineno, called name) for every filesystem write in `tree`."""
    return sorted(
        (node.lineno, name)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (name := _find_write_call_name(node)) is not None
    )


def _find_write_call_name(node: ast.Call) -> str | None:
    name = _called_name(node.func)
    if name in _WRITE_CALLS:
        return name
    # `open(p, "w")` and `p.open("a")` put bytes on disk without naming a write.
    # The events.jsonl log reached master through exactly this hole.
    if name == "open" and _WRITING_MODES.intersection(_find_open_mode(node)):
        return "open"
    return None


def _find_open_mode(node: ast.Call) -> str:
    positional = node.args[1:] if isinstance(node.func, ast.Name) else node.args[:1]
    mode = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
    for candidate in (mode, *positional):
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value
    return ""


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def test_only_frames_exports_uploads_and_published_artifacts_touch_the_disk() -> None:
    offenders: list[str] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWED_WRITERS or "_arch_tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [f"{rel}:{line}: {name}(...)" for line, name in find_file_writes(tree)]
    assert not offenders, (
        "a project's state belongs in the document store (app/core/persistence.py) "
        "or in a frame (app/core/frames.py) — nothing else under app/ writes a "
        "file. If this is genuinely one of the exempt kinds (a frame, an export "
        "the user downloads, a file the user uploaded), add it to "
        "_ALLOWED_WRITERS with the reason; otherwise the state wants a "
        "PersistedModel:\n  " + "\n  ".join(offenders)
    )


def test_every_allowed_writer_exists_and_actually_writes() -> None:
    stale = [rel for rel in _ALLOWED_WRITERS if not (_REPO_ROOT / rel).is_file()]
    # A stale entry turns the rule off for a file nobody is checking any more; an
    # entry that no longer writes anything is a rule pretending to be needed.
    assert not stale, f"stale _ALLOWED_WRITERS entry: {stale}"
    idle = [
        rel for rel in _ALLOWED_WRITERS
        if not find_file_writes(ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8")))
    ]
    assert not idle, f"_ALLOWED_WRITERS entry that no longer writes anything: {idle}"


def test_the_detector_sees_the_shapes_a_caller_actually_writes() -> None:
    # Else the rule above passes on a detector that matches nothing.
    for snippet in (
        'path.write_text("x", encoding="utf-8")\n',
        "path.write_bytes(content)\n",
        "shutil.copyfileobj(src, out)\n",
        "shutil.rmtree(target)\n",
        "target.unlink()\n",
        'with open(path, "w", encoding="utf-8") as fh:\n    fh.write(text)\n',
        'with path.open("a", encoding="utf-8") as fh:\n    fh.write(line)\n',
        'with open(path, mode="wb") as fh:\n    fh.write(data)\n',
    ):
        assert find_file_writes(ast.parse(snippet)), snippet
    for allowed in (
        "record.save()\n",
        "get_store().write(collection, id, data)\n",
        "text = path.read_text(encoding='utf-8')\n",
        # A directory holds no state, and the run dir is still made to hold frames.
        "(root / 'a').mkdir(parents=True, exist_ok=True)\n",
        'with open(path, encoding="utf-8") as fh:\n    text = fh.read()\n',
        'with path.open("rb") as fh:\n    data = fh.read()\n',
    ):
        assert not find_file_writes(ast.parse(allowed)), allowed
