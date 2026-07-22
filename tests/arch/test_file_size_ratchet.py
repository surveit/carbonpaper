"""Architecture: a file-size ratchet on ``app/``.

The cyclomatic-complexity ratchet (``test_complexity_ratchet.py``) gates how
tangled one function is allowed to get, but a file can stay full of small,
simple functions and still grow past what a reviewer can hold in their head
in one sitting — a dimension complexity alone never measures. This rule
gates that dimension directly: a ``.py`` file's physical line count.

Scope is identical to the complexity ratchet's: this module reuses its
``find_app_source_files`` scanner (imported, not duplicated) rather than
re-deriving the same exemption rules a second time.

Unlike the complexity ratchet, there is no JSON baseline file recording an
exact value per offender — line counts churn on nearly every unrelated edit
to a file, so an exact-match baseline would drift constantly and demand
constant re-recording for no safety benefit. Instead:

1. A file over ``_LINE_CEILING`` and not in ``_ALLOWLIST`` below is a new
   offender — split it into smaller modules.
2. A file in ``_ALLOWLIST`` that has grown past ``_LINE_CEILING *
   _BACKSTOP_MULTIPLIER`` is a backstop violation: grandfathering a file in
   is not a license to let it grow unboundedly while listed.
3. An ``_ALLOWLIST`` entry for a file now at or under ``_LINE_CEILING`` (or a
   file that no longer exists) is stale — remove the entry. The allowlist is
   a ratchet: entries may only be removed, never added.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arch.test_complexity_ratchet import find_app_source_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_LINE_CEILING = 800
_BACKSTOP_MULTIPLIER = 2

# Pre-existing files over `_LINE_CEILING`, grandfathered in rather than
# forced into an immediate split. A ratchet: entries may only be removed,
# never added — a new offender must be split into modules, not listed here.
# Each entry names why it's here.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # ~1057 lines at the time this rule was added. A planned split of the
        # runner into smaller modules will empty this list.
        "app/runtime/runner.py",
    }
)


@dataclass(frozen=True)
class FileSize:
    """One file's measured physical line count.

    ``path`` is repo-relative with forward slashes (identical on Windows and
    CI Linux), matching `_ALLOWLIST`'s entries so the two compare directly.
    """

    path: str
    lines: int


def measure_file_sizes(paths: list[Path], repo_root: Path) -> list[FileSize]:
    """Every file in `paths` at its measured physical line count."""
    return [
        FileSize(path.relative_to(repo_root).as_posix(), _count_lines(path))
        for path in paths
    ]


def find_ratchet_violations(sizes: list[FileSize], allowlist: frozenset[str]) -> list[str]:
    """Human-readable offender lines for the three ratchet rules in the
    module docstring, run over `sizes` against `allowlist`."""
    by_path = {size.path: size for size in sizes}
    offenders = [
        _describe_new_violation(size)
        for size in sizes
        if size.lines > _LINE_CEILING and size.path not in allowlist
    ]
    offenders += [
        _describe_backstop_violation(by_path[entry])
        for entry in sorted(allowlist)
        if entry in by_path and by_path[entry].lines > _LINE_CEILING * _BACKSTOP_MULTIPLIER
    ]
    offenders += [
        _describe_stale_entry(entry, by_path.get(entry))
        for entry in sorted(allowlist)
        if entry not in by_path or by_path[entry].lines <= _LINE_CEILING
    ]
    return offenders


def test_files_do_not_exceed_the_file_size_ratchet() -> None:
    sizes = measure_file_sizes(find_app_source_files(_APP_ROOT), _REPO_ROOT)
    offenders = find_ratchet_violations(sizes, _ALLOWLIST)
    assert not offenders, (
        f"file-size ratchet: a file over {_LINE_CEILING} physical lines must be split into "
        "smaller modules, or — if pre-existing — named in the _ALLOWLIST frozenset in "
        "tests/arch/test_file_size_ratchet.py; the allowlist may only shrink, never grow, and "
        f"an allowlisted file may not exceed {_LINE_CEILING * _BACKSTOP_MULTIPLIER} lines "
        f"({_BACKSTOP_MULTIPLIER}x the ceiling) even while listed:\n  " + "\n  ".join(offenders)
    )


# --- measurement ---------------------------------------------------------


def _count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


# --- offender messages -----------------------------------------------------


def _describe_new_violation(size: FileSize) -> str:
    return (
        f"{size.path}  lines={size.lines} (> {_LINE_CEILING}, not in _ALLOWLIST) — split it "
        "into smaller modules; the allowlist must never grow"
    )


def _describe_backstop_violation(size: FileSize) -> str:
    return (
        f"{size.path}  lines={size.lines} (> {_LINE_CEILING * _BACKSTOP_MULTIPLIER}, the "
        f"{_BACKSTOP_MULTIPLIER}x backstop for an allowlisted file) — split it into smaller "
        "modules now; grandfathering is not a license to grow unboundedly"
    )


def _describe_stale_entry(path: str, size: FileSize | None) -> str:
    reason = "the file no longer exists" if size is None else f"it now measures {size.lines} (<= {_LINE_CEILING})"
    return f"{path}  ({reason}) — remove the stale _ALLOWLIST entry"


# --- unit tests for the checker, on tmp_path fixtures (red + green) -------


def test_find_ratchet_violations_flags_an_unlisted_file_over_the_ceiling() -> None:
    over = FileSize("app/big.py", _LINE_CEILING + 1)
    offenders = find_ratchet_violations([over], frozenset())
    assert len(offenders) == 1
    assert "app/big.py" in offenders[0] and "split" in offenders[0]


def test_find_ratchet_violations_passes_a_file_under_the_ceiling() -> None:
    under = FileSize("app/small.py", _LINE_CEILING - 1)
    assert find_ratchet_violations([under], frozenset()) == []


def test_find_ratchet_violations_passes_an_allowlisted_file_over_the_ceiling() -> None:
    over = FileSize("app/big.py", _LINE_CEILING + 1)
    assert find_ratchet_violations([over], frozenset({"app/big.py"})) == []


def test_find_ratchet_violations_flags_an_allowlisted_file_past_the_backstop() -> None:
    past_backstop = FileSize("app/big.py", _LINE_CEILING * _BACKSTOP_MULTIPLIER + 1)
    offenders = find_ratchet_violations([past_backstop], frozenset({"app/big.py"}))
    assert len(offenders) == 1
    assert "app/big.py" in offenders[0] and "backstop" in offenders[0]


def test_find_ratchet_violations_passes_an_allowlisted_file_exactly_at_the_backstop() -> None:
    at_backstop = FileSize("app/big.py", _LINE_CEILING * _BACKSTOP_MULTIPLIER)
    assert find_ratchet_violations([at_backstop], frozenset({"app/big.py"})) == []


def test_find_ratchet_violations_flags_a_stale_entry_when_now_under_the_ceiling() -> None:
    shrunk = FileSize("app/big.py", _LINE_CEILING - 1)
    offenders = find_ratchet_violations([shrunk], frozenset({"app/big.py"}))
    assert len(offenders) == 1
    assert f"<= {_LINE_CEILING}" in offenders[0] and "remove" in offenders[0]


def test_find_ratchet_violations_flags_a_stale_entry_when_the_file_is_gone() -> None:
    offenders = find_ratchet_violations([], frozenset({"app/gone.py"}))
    assert len(offenders) == 1
    assert "no longer exists" in offenders[0] and "remove" in offenders[0]


def test_measure_file_sizes_reads_relative_posix_path_and_counts_physical_lines(tmp_path: Path) -> None:
    nested = tmp_path / "app" / "sub"
    nested.mkdir(parents=True)
    file = nested / "m.py"
    file.write_text("a = 1\nb = 2\nc = 3\n")
    [size] = measure_file_sizes([file], tmp_path)
    assert size.path == "app/sub/m.py"
    assert size.lines == 3


def test_measure_file_sizes_counts_a_file_with_no_trailing_newline(tmp_path: Path) -> None:
    file = tmp_path / "m.py"
    file.write_text("a = 1\nb = 2")
    [size] = measure_file_sizes([file], tmp_path)
    assert size.lines == 2
