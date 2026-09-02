"""Architecture: a PersistedModel's `id` is minted, never composed.
The key a record is looked up by is a FIELD; `id` stays the uuid4 it defaults to.
`_GRANDFATHERED` is what the code passes TODAY, not the target — it MAY ONLY SHRINK.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch import find_governed_files
from arch._helpers import find_subclasses_of, parse_module

_PERSISTED_MODEL = "PersistedModel"

# Construction sites that pass `id=`, as `module.py::Model`. Each is a record whose
# id carries its own data; unpicking one means moving that data to a field and
# giving readers a lookup that filters on it. Never add to this.
_GRANDFATHERED: frozenset[str] = frozenset({
    "store.py::AgentSession",
    # The deliberate exception, and the only principled one: a cache entry IS its
    # content hash, so looking it up by anything else would mean reading it first.
    "stage_cache.py::StageCacheEntry",
    "citations.py::StageCitations",
    "manifest.py::RunManifest",
    "run_log.py::RunEventChunk",
    "human_review_queue.py::QueueFingerprints",
    "drafts.py::Draft",
    "loader.py::WorkingCopy",
    "methodology.py::Methodology",
    "project.py::Project",
    # Composes the scope prefix the store's prefix-selected list() needs, then a
    # uuid — the closest of these to the rule, and still not it.
    "run_manifest_metadata.py::RunManifestMetadata",
    "terms.py::StoredTerms",
    "versioning.py::WorkflowVersion",
    # A staging step, approved on the record: the fix is project_id as a field and find().
    "store.py::EvalRun",
    "project.py::EvalConfig",
})


def find_composed_id_offenders(paths: list[Path]) -> list[str]:
    """Every site passing `id=` to a PersistedModel subclass, minus the grandfathered."""
    models = find_persisted_model_names(paths)
    offenders: list[str] = []
    for path in paths:
        for node in _find_calls(parse_module(path)):
            name = _called_name(node)
            if name not in models or not _passes_id(node):
                continue
            entry = f"{path.name}::{name}"
            if entry not in _GRANDFATHERED:
                offenders.append(f"{path.name}:{node.lineno}  {name}(id=...)")
    return offenders


def find_persisted_model_names(paths: list[Path]) -> set[str]:
    return {
        node.name
        for path in paths
        for node in find_subclasses_of(parse_module(path), _PERSISTED_MODEL)
    }


def _find_calls(tree: ast.Module) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _passes_id(node: ast.Call) -> bool:
    return any(kw.arg == "id" for kw in node.keywords)


def test_no_new_record_composes_its_own_id() -> None:
    offenders = find_composed_id_offenders(find_governed_files(__file__))
    assert not offenders, (
        "a PersistedModel's `id` is opaque — let it default to uuid4().hex and put the "
        "key readers look the record up by in a FIELD (`project_id`, `sha256`), with a "
        "lookup that filters on that field. An id built from the record's own data gives "
        "it two identities that must agree, which nothing checks, and re-keying it means "
        "deleting and rewriting the row instead of editing a field. Adding an entry to "
        "_GRANDFATHERED in this file is not the fix — that set may only shrink:\n  "
        + "\n  ".join(offenders)
    )


def test_every_grandfathered_entry_still_exists() -> None:
    """A stale exemption silently widens the rule, so the set has to shrink as sites are fixed."""
    paths = find_governed_files(__file__)
    models = find_persisted_model_names(paths)
    live = {
        f"{path.name}::{_called_name(node)}"
        for path in paths
        for node in _find_calls(parse_module(path))
        if _called_name(node) in models and _passes_id(node)
    }
    assert not (_GRANDFATHERED - live), (
        "these no longer pass `id=` — delete them from _GRANDFATHERED:\n  "
        + "\n  ".join(sorted(_GRANDFATHERED - live))
    )


# --- unit tests for the checker, on inline snippets (red + green) ----------
def _write(tmp_path: Path, source: str) -> Path:
    target = tmp_path / "models.py"
    target.write_text(source, encoding="utf-8")
    return target


def test_the_checker_flags_a_composed_id(tmp_path: Path) -> None:
    target = _write(tmp_path, (
        "class Thing(PersistedModel):\n    project_id: str\n\n"
        "def store(pid):\n    Thing(id=pid, project_id=pid).save()\n"
    ))
    assert find_composed_id_offenders([target]) == ["models.py:5  Thing(id=...)"]


def test_the_checker_passes_a_minted_id(tmp_path: Path) -> None:
    target = _write(tmp_path, (
        "class Thing(PersistedModel):\n    project_id: str\n\n"
        "def store(pid):\n    Thing(project_id=pid).save()\n"
    ))
    assert find_composed_id_offenders([target]) == []


def test_the_checker_ignores_a_non_record(tmp_path: Path) -> None:
    target = _write(tmp_path, "def store(pid):\n    StageInput(id=pid)\n")
    assert find_composed_id_offenders([target]) == []
