"""Scope taken from location: an arch test in an ``_arch_tests/`` folder governs the
subtree it sits in — no hardcoded path. Exemptions are checked on the path RELATIVE
to the scan base: the checkout may itself live under a hidden directory (e.g. a git
worktree under ``.claude/``), whose absolute parts would match ``startswith(".")``.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MARKER = "_arch_tests"
_EXEMPT_PARTS = {"tests", _MARKER, "__pycache__", "_vendor", "node_modules", "venv"}
# Wider than _EXEMPT_PARTS: prose rules govern tests and docs too, not just app code.
_EXEMPT_TEXT_PARTS = {"__pycache__", "_vendor", "node_modules", "venv"}


def find_governed_files(test_file: str) -> list[Path]:
    feature_dir = _resolve_feature_dir(test_file)
    files = list(_iter_source_under(feature_dir))
    if not files:
        raise ValueError(
            f"architecture test {test_file} governs no source files under "
            f"{feature_dir} — an _arch_tests/ folder must sit alongside the code it "
            "checks; write the code before the rule can pass"
        )
    return files


def scan_all_source() -> list[Path]:
    files = list(_iter_source_under(_REPO_ROOT))
    if not files:
        raise ValueError(
            f"scan_all_source found no source files under {_REPO_ROOT} — the scope "
            "resolver is misconfigured (exemptions are excluding everything)"
        )
    return files


def scan_all_text(suffixes: tuple[str, ...]) -> list[Path]:
    files = sorted(
        path
        for suffix in suffixes
        for path in _REPO_ROOT.rglob(f"*{suffix}")
        if not any(
            part.startswith(".") or part in _EXEMPT_TEXT_PARTS
            for part in path.relative_to(_REPO_ROOT).parts
        )
    )
    if not files:
        raise ValueError(
            f"scan_all_text found no {suffixes} files under {_REPO_ROOT} — the "
            "scope resolver is misconfigured"
        )
    return files


def find_source_files_under(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    files = list(_iter_source_under(target))
    if not files:
        raise ValueError(
            f"architecture test targets {target}, which governs no source files — "
            "check the target path and the exempt parts"
        )
    return files


def _resolve_feature_dir(test_file: str) -> Path:
    for parent in Path(test_file).resolve().parents:
        if parent.name == _MARKER:
            return parent.parent
    raise ValueError(f"{test_file} must live inside an '{_MARKER}/' folder")


def _iter_source_under(base: Path) -> Iterator[Path]:
    if not base.exists():
        raise FileNotFoundError(f"architecture test targets a missing path: {base}")
    for path in sorted(base.rglob("*.py")):
        if _is_source(path.relative_to(base)):
            yield path


def _is_source(relative_path: Path) -> bool:
    return not any(
        part.startswith(".") or part in _EXEMPT_PARTS for part in relative_path.parts
    )
