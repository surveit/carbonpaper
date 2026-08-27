"""Architecture: no `project_dir` key under stages, and only a named module writes."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from arch import check_no_dict_keys
from arch._helpers import collect_called_methods, parse_module
from arch.scope import find_source_files_under

_RUNTIME_STAGES_DIR = Path(__file__).resolve().parents[1] / "stages"
_RUNTIME_DIR = Path(__file__).resolve().parents[1]

_BANNED_PERSISTENCE_METHODS = frozenset({"save", "delete"})

# Denied until listed, per tests/arch/test_contracts_are_whitelists.py.
_MAY_WRITE_A_RUN_RECORD: Mapping[str, str] = {
    "citations.py": "StageCitations — what a report stage cited, per run",
    "manifest.py": "RunManifest — the run's own record",
    "run_log.py": "RunLogChunk — the run's event log",
    "human_review_queue.py": "the queue this run halted on",
    "workflow_outputs.py": "WorkflowOutput — what this run published",
}


def find_persisted_write_call_offenders(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        if path.name in _MAY_WRITE_A_RUN_RECORD:
            continue
        hits = collect_called_methods(parse_module(path)) & _BANNED_PERSISTENCE_METHODS
        if hits:
            offenders.append(f"{path.name}: {sorted(hits)}")
    return offenders


def test_stage_modules_never_hold_a_project_dir_key() -> None:
    offenders = check_no_dict_keys(find_source_files_under(_RUNTIME_STAGES_DIR), {"project_dir"})
    assert not offenders, (
        "a stage handler must reach project scope only through RunContext's own "
        "fields (identity, stage_cache) — never a `\"project_dir\"` dict key "
        "stashed on the side:\n  " + "\n  ".join(offenders)
    )


def test_runtime_never_calls_save_or_delete_directly() -> None:
    offenders = find_persisted_write_call_offenders(find_source_files_under(_RUNTIME_DIR))
    assert not offenders, (
        "app/runtime must reach a cross-run write only through the cache seam's "
        "own StageCache.put — never PersistedModel.save()/delete() directly, "
        "even when it holds a legitimately-read entry instance:\n  "
        + "\n  ".join(offenders)
    )


# --- unit tests for find_persisted_write_call_offenders, on inline snippets ---


def test_find_persisted_write_call_offenders_flags_a_save_call(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("entry.save()\n", encoding="utf-8")
    assert find_persisted_write_call_offenders([target]) == ["m.py: ['save']"]


def test_find_persisted_write_call_offenders_flags_a_delete_call(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("Entry.delete(entry_id)\n", encoding="utf-8")
    assert find_persisted_write_call_offenders([target]) == ["m.py: ['delete']"]


def test_find_persisted_write_call_offenders_ignores_read_only_calls(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text(
        "cache.get(project, sid, fp, ifp)\ncache.find_entries(project, sid, fp)\n",
        encoding="utf-8",
    )
    assert find_persisted_write_call_offenders([target]) == []


def test_a_listed_module_may_write(tmp_path: Path) -> None:
    target = tmp_path / "workflow_outputs.py"
    target.write_text("WorkflowOutput(...).save()\n", encoding="utf-8")
    assert find_persisted_write_call_offenders([target]) == []


def test_an_unlisted_module_may_not_write_even_a_run_scoped_record(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text(
        "from app.models.records.workflow_output import WorkflowOutput\n"
        "WorkflowOutput(...).save()\n",
        encoding="utf-8",
    )
    assert find_persisted_write_call_offenders([target]) == ["m.py: ['save']"]
