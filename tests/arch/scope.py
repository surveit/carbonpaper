"""Location-derived scope for architecture tests.

An architecture test lives in an ``_arch_tests/`` folder inside the code it governs.
``find_governed_files(__file__)`` returns the source files in that folder's parent
subtree, so a test declares its scope by where it sits — no hardcoded path.
``scan_all_source()`` is the whole-repo scope for genuinely-global rules.

Exemptions are checked on the path RELATIVE to the scan base, not the absolute path:
the checkout may itself live under a hidden directory (e.g. a git worktree under
``.claude/``), so absolute parts would spuriously match ``startswith(".")``.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MARKER = "_arch_tests"
_EXEMPT_PARTS = {"tests", _MARKER, "__pycache__", "_vendor", "node_modules", "venv"}


def find_governed_files(test_file: str) -> list[Path]:
    """Source ``.py`` files in the folder this arch test lives in, and below."""
    feature_dir = _resolve_feature_dir(test_file)
    return list(_iter_source_under(feature_dir))


def scan_all_source() -> list[Path]:
    """Every source ``.py`` in the repo — whole repo minus the exemptions."""
    return list(_iter_source_under(_REPO_ROOT))


def _resolve_feature_dir(test_file: str) -> Path:
    """The directory an ``_arch_tests/`` folder sits in, walking up from the test."""
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
    """True if a base-relative path is first-party source subject to arch rules."""
    return not any(
        part.startswith(".") or part in _EXEMPT_PARTS for part in relative_path.parts
    )
