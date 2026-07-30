"""Architecture: a file-size ratchet on ``app/`` — radon LLOC. Three rules:
1. over ``_LLOC_CEILING`` and not in ``_ALLOWLIST`` — a new offender, split it;
2. allowlisted but past ``_LLOC_CEILING * _BACKSTOP_MULTIPLIER`` — backstop violation;
3. allowlisted while at/under the ceiling (or gone) — stale entry, remove it.
The only fix for rule 1 is splitting the module; see `_describe_new_violation`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from radon.raw import analyze

from arch.test_complexity_ratchet import find_app_source_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
# Logical lines, not physical: LLOC counts statements, so it does not move when a
# formatter re-wraps a call across lines, and it does not bill comments against a
# file's budget. 400 admits every file under app/ today (largest: 386).
_LLOC_CEILING = 400
_BACKSTOP_MULTIPLIER = 2

# Pre-existing files over `_LLOC_CEILING`, grandfathered in rather than
# forced into an immediate split. A ratchet: entries may only be removed,
# never added — a new offender must be split into modules, not listed here.
# Each entry names why it's here.
#
# Empty: app/runtime/runner.py (~1057 physical lines when this rule was added)
# was the sole entry, grandfathered pending a planned split. That split
# landed (runner.py now holds only the production entry points; the shared
# execution engine moved to app/runtime/executor.py), so no file under app/
# currently exceeds the ceiling and the allowlist has nothing to carry.
_ALLOWLIST: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FileSize:
    """One file's measured logical line count.

    ``path`` is repo-relative with forward slashes (identical on Windows and
    CI Linux), matching `_ALLOWLIST`'s entries so the two compare directly.
    """

    path: str
    lloc: int


def measure_file_sizes(paths: list[Path], repo_root: Path) -> list[FileSize]:
    """Every file in `paths` at its measured logical line count."""
    return [
        FileSize(path.relative_to(repo_root).as_posix(), _count_logical_lines(path))
        for path in paths
    ]


def find_ratchet_violations(sizes: list[FileSize], allowlist: frozenset[str]) -> list[str]:
    """Human-readable offender lines for the three ratchet rules in the
    module docstring, run over `sizes` against `allowlist`."""
    by_path = {size.path: size for size in sizes}
    offenders = [
        _describe_new_violation(size)
        for size in sizes
        if size.lloc > _LLOC_CEILING and size.path not in allowlist
    ]
    offenders += [
        _describe_backstop_violation(by_path[entry])
        for entry in sorted(allowlist)
        if entry in by_path and by_path[entry].lloc > _LLOC_CEILING * _BACKSTOP_MULTIPLIER
    ]
    offenders += [
        _describe_stale_entry(entry, by_path.get(entry))
        for entry in sorted(allowlist)
        if entry not in by_path or by_path[entry].lloc <= _LLOC_CEILING
    ]
    return offenders


def test_files_do_not_exceed_the_file_size_ratchet() -> None:
    sizes = measure_file_sizes(find_app_source_files(_APP_ROOT), _REPO_ROOT)
    offenders = find_ratchet_violations(sizes, _ALLOWLIST)
    assert not offenders, (
        f"file-size ratchet: a file over {_LLOC_CEILING} logical lines (radon LLOC) must be "
        "split into smaller modules, or — if pre-existing — named in the _ALLOWLIST frozenset "
        "in tests/arch/test_file_size_ratchet.py; the allowlist may only shrink, never grow, "
        f"and an allowlisted file may not exceed {_LLOC_CEILING * _BACKSTOP_MULTIPLIER} LLOC "
        f"({_BACKSTOP_MULTIPLIER}x the ceiling) even while listed:\n  " + "\n  ".join(offenders)
    )


# --- measurement ---------------------------------------------------------


def _count_logical_lines(path: Path) -> int:
    return int(analyze(path.read_text(encoding="utf-8")).lloc)


# --- offender messages -----------------------------------------------------


def _describe_new_violation(size: FileSize) -> str:
    """The remedy this names is deliberately narrow: the ceiling exists to force a
    split, so squeezing back under it any other way defeats the rule."""
    return (
        f"{size.path}  lloc={size.lloc} (> {_LLOC_CEILING}, not in _ALLOWLIST) — split it "
        "into smaller modules; the allowlist must never grow.\n"
        "      The fix is a SPLIT, not a squeeze. LLOC counts statements, so the only way "
        "to shrink one without moving code out is to write denser code: fusing several "
        "named steps into one long expression, dropping an intermediate variable that was "
        "carrying a name, inlining a small helper back into its caller. Do not. That "
        "trades the readability this ceiling exists to protect, and leaves the next change "
        "in the same trap with less room.\n"
        "      Instead, move a cohesive group out to its own module (and if that is too "
        "large for the change you are on, say so and stop: the split is the task, and a "
        "human decides whether to take it now)."
    )


def _describe_backstop_violation(size: FileSize) -> str:
    return (
        f"{size.path}  lloc={size.lloc} (> {_LLOC_CEILING * _BACKSTOP_MULTIPLIER}, the "
        f"{_BACKSTOP_MULTIPLIER}x backstop for an allowlisted file) — split it into smaller "
        "modules now; grandfathering is not a license to grow unboundedly"
    )


def _describe_stale_entry(path: str, size: FileSize | None) -> str:
    reason = "the file no longer exists" if size is None else f"it now measures {size.lloc} (<= {_LLOC_CEILING})"
    return f"{path}  ({reason}) — remove the stale _ALLOWLIST entry"


# --- unit tests for the checker, on tmp_path fixtures (red + green) -------


def test_find_ratchet_violations_flags_an_unlisted_file_over_the_ceiling() -> None:
    over = FileSize("app/big.py", _LLOC_CEILING + 1)
    offenders = find_ratchet_violations([over], frozenset())
    assert len(offenders) == 1
    assert "app/big.py" in offenders[0] and "split" in offenders[0]


def test_find_ratchet_violations_passes_a_file_under_the_ceiling() -> None:
    under = FileSize("app/small.py", _LLOC_CEILING - 1)
    assert find_ratchet_violations([under], frozenset()) == []


def test_find_ratchet_violations_passes_an_allowlisted_file_over_the_ceiling() -> None:
    over = FileSize("app/big.py", _LLOC_CEILING + 1)
    assert find_ratchet_violations([over], frozenset({"app/big.py"})) == []


def test_find_ratchet_violations_flags_an_allowlisted_file_past_the_backstop() -> None:
    past_backstop = FileSize("app/big.py", _LLOC_CEILING * _BACKSTOP_MULTIPLIER + 1)
    offenders = find_ratchet_violations([past_backstop], frozenset({"app/big.py"}))
    assert len(offenders) == 1
    assert "app/big.py" in offenders[0] and "backstop" in offenders[0]


def test_find_ratchet_violations_passes_an_allowlisted_file_exactly_at_the_backstop() -> None:
    at_backstop = FileSize("app/big.py", _LLOC_CEILING * _BACKSTOP_MULTIPLIER)
    assert find_ratchet_violations([at_backstop], frozenset({"app/big.py"})) == []


def test_find_ratchet_violations_flags_a_stale_entry_when_now_under_the_ceiling() -> None:
    shrunk = FileSize("app/big.py", _LLOC_CEILING - 1)
    offenders = find_ratchet_violations([shrunk], frozenset({"app/big.py"}))
    assert len(offenders) == 1
    assert f"<= {_LLOC_CEILING}" in offenders[0] and "remove" in offenders[0]


def test_find_ratchet_violations_flags_a_stale_entry_when_the_file_is_gone() -> None:
    offenders = find_ratchet_violations([], frozenset({"app/gone.py"}))
    assert len(offenders) == 1
    assert "no longer exists" in offenders[0] and "remove" in offenders[0]


def test_measure_file_sizes_reads_relative_posix_path_and_counts_logical_lines(tmp_path: Path) -> None:
    nested = tmp_path / "app" / "sub"
    nested.mkdir(parents=True)
    file = nested / "m.py"
    file.write_text("a = 1\nb = 2\nc = 3\n")
    [size] = measure_file_sizes([file], tmp_path)
    assert size.path == "app/sub/m.py"
    assert size.lloc == 3


def test_measure_file_sizes_does_not_bill_comments_or_blank_lines(tmp_path: Path) -> None:
    file = tmp_path / "m.py"
    file.write_text("# note\n\na = 1\n\n# more\nb = 2\n")
    [size] = measure_file_sizes([file], tmp_path)
    assert size.lloc == 2


def test_measure_file_sizes_is_unchanged_by_reformatting(tmp_path: Path) -> None:
    """The ceiling must not move when a formatter re-wraps a call across lines."""
    compact = tmp_path / "compact.py"
    compact.write_text("run(a, b, c, d)\n")
    exploded = tmp_path / "exploded.py"
    exploded.write_text("run(\n    a,\n    b,\n    c,\n    d,\n)\n")
    [one] = measure_file_sizes([compact], tmp_path)
    [other] = measure_file_sizes([exploded], tmp_path)
    assert one.lloc == other.lloc == 1
