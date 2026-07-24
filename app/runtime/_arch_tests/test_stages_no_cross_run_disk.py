"""Architecture: no ambient persistence in runtime — a run's products persist
only through a designated seam, never by runtime code reaching storage itself.

Everything a run produces is now designated: its manifest is a RUN-scoped
`RunManifest` document and its stage outputs / review-queue snapshots are
frames, all persisted through the run-persistence service
(`app.services.run_store`, which owns `FrameStore` + `RunManifest.save`). So no
file under `app/runtime` mints a run product itself — not a `PersistedModel`
write, not an ad-hoc parquet frame. (An `input_data` handler still *reads* its
source file, and the publish handler mkdirs an `artifacts/` dir for the
authored function to write into; reads and dir creation are not persistence.)

Three rules:
  1. No file under `app/runtime/stages` reads or writes a `"project_dir"` dict
     key. This was the shape of the TRANSITIONAL registry a stage handler
     used, pre-cache, to reach a project's own directory on the side (keyed
     off a module-global dict rather than anything RunContext carries); this
     rule keeps that channel from coming back.
  2. No file under `app/runtime` calls `.save()` or `.delete()` — the two
     `PersistedModel` writes (`app.core.persistence`) — directly. A stage
     handler that reached `get_store()` for itself, or called `.save()` on a
     `StageCacheEntry` it merely read, would defeat the seam's whole point:
     the stage-result cache is written only through `StageCache.put`, and the
     run manifest only through `run_store.persist_manifest`, never by runtime
     code reaching past the accessor it was handed.
  3. No file under `app/runtime` calls `.to_parquet(...)` — an ad-hoc frame
     write straight to disk. A stage output or a queue snapshot is a frame, and
     a frame persists through `run_store`'s `FrameStore`-backed helpers
     (`save_output_frame` / `save_queue_snapshot`), which validate the id and
     own the CSV fallback — never a bare `df.to_parquet(run_dir / ...)`.
"""
from __future__ import annotations

from pathlib import Path

from arch import check_no_dict_keys
from arch._helpers import collect_called_methods, parse_module
from arch.scope import find_source_files_under

_RUNTIME_STAGES_DIR = Path(__file__).resolve().parents[1] / "stages"
_RUNTIME_DIR = Path(__file__).resolve().parents[1]

_BANNED_PERSISTENCE_METHODS = frozenset({"save", "delete"})
# `to_parquet` always writes a file (it has no string-return mode), so banning
# the method name is unambiguous. `to_csv` is deliberately NOT banned: with no
# path argument it returns a string, a legitimate non-persisting use, so a
# name-based ban would flag false positives.
_BANNED_FRAME_WRITE_METHODS = frozenset({"to_parquet"})


def find_persisted_write_call_offenders(paths: list[Path]) -> list[str]:
    """"<path>: [<method>, ...]" for every file in `paths` that calls `.save()`
    or `.delete()` on anything — the two `PersistedModel` writes — directly."""
    offenders: list[str] = []
    for path in paths:
        hits = collect_called_methods(parse_module(path)) & _BANNED_PERSISTENCE_METHODS
        if hits:
            offenders.append(f"{path.name}: {sorted(hits)}")
    return offenders


def find_frame_write_offenders(paths: list[Path]) -> list[str]:
    """"<path>: [<method>, ...]" for every file in `paths` that calls
    `.to_parquet(...)` — an ad-hoc frame write bypassing the run-frame seam."""
    offenders: list[str] = []
    for path in paths:
        hits = collect_called_methods(parse_module(path)) & _BANNED_FRAME_WRITE_METHODS
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


def test_runtime_never_writes_frames_directly() -> None:
    offenders = find_frame_write_offenders(find_source_files_under(_RUNTIME_DIR))
    assert not offenders, (
        "app/runtime must persist a stage output or queue snapshot only through "
        "the run-frame seam (app.services.run_store's save_output_frame / "
        "save_queue_snapshot, backed by FrameStore) — never a bare "
        "df.to_parquet(...) straight to the run directory:\n  "
        + "\n  ".join(offenders)
    )


# --- unit tests for find_persisted_write_call_offenders, on inline snippets ---


def test_find_persisted_write_call_offenders_flags_a_save_call(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("entry.save()\n")
    assert find_persisted_write_call_offenders([target]) == ["m.py: ['save']"]


def test_find_persisted_write_call_offenders_flags_a_delete_call(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("Entry.delete(entry_id)\n")
    assert find_persisted_write_call_offenders([target]) == ["m.py: ['delete']"]


def test_find_persisted_write_call_offenders_ignores_read_only_calls(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("cache.get(project, sid, fp, ifp)\ncache.find_entries(project, sid, fp)\n")
    assert find_persisted_write_call_offenders([target]) == []


def test_find_frame_write_offenders_flags_a_to_parquet_call(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("df.to_parquet(run_dir / 'outputs' / 'x.parquet')\n")
    assert find_frame_write_offenders([target]) == ["m.py: ['to_parquet']"]


def test_find_frame_write_offenders_ignores_to_csv_string_use(tmp_path: Path) -> None:
    """`to_csv` with no path returns a string (an HTTP body, say) — a
    non-persisting use, so it is deliberately not banned."""
    target = tmp_path / "m.py"
    target.write_text("body = df.to_csv(index=False)\n")
    assert find_frame_write_offenders([target]) == []


def test_find_frame_write_offenders_ignores_frame_reads(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("df = pd.read_parquet(path)\ndf2 = run_store.load_queue_snapshot(rd, sid)\n")
    assert find_frame_write_offenders([target]) == []
