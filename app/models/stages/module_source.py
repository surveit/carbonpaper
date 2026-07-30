"""Digest of the source behind a `function.kind=module` handle — the pin that
makes WHICH code a stage runs part of its identity. Resolution mirrors the
runtime's `importlib.import_module`: repo-relative file first, then the import
system. Never degrades to hashing the module path: an unresolvable or unreadable
module raises."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.core.paths import repo_root
from app.core.utils import compute_short_hash


def compute_module_source_digest(module: str) -> str:
    """compute_short_hash of the module's source; raises ValueError naming an
    unresolvable or unreadable module."""
    path = resolve_module_source_path(module)
    if path is None:
        raise ValueError(
            f"function.module '{module}' cannot be resolved to a source file, so the "
            f"code this stage runs cannot be pinned. Make the module importable from "
            f"the repo root, or use function.kind=inline."
        )
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"function.module '{module}': cannot read source at {path}: {exc}")
    return compute_short_hash(source)


def verify_pinned_module_digest(module: str, pinned: str) -> None:
    """Raise ValueError unless the module's source still hashes to `pinned`."""
    actual = compute_module_source_digest(module)
    if actual != pinned:
        raise ValueError(
            f"function.module '{module}' has changed since this stage definition was "
            f"written: it pins module_digest {pinned}, the source now hashes to {actual}. "
            f"Refusing to run — cached results and this version's frozen behaviour were "
            f"produced by the pinned source. Set function.module_digest to {actual} to "
            f"adopt the new code."
        )


def resolve_module_source_path(module: str) -> Path | None:
    """The .py file backing a dotted module ref — repo-relative, else via the
    import system — or None if it cannot be located."""
    if not module:
        return None
    parts = module.split(".")
    for candidate in (
        repo_root() / Path(*parts).with_suffix(".py"),
        repo_root() / Path(*parts) / "__init__.py",
    ):
        if candidate.exists():
            return candidate
    return _find_imported_module_path(module)


def _find_imported_module_path(module: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        # find_spec raises for a missing/invalid parent package, or a module
        # whose parent has no __path__ — both mean "not resolvable here".
        return None
    origin = spec.origin if spec is not None else None
    return Path(origin) if origin and origin.endswith(".py") else None
