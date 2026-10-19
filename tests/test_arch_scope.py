from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from arch import find_banned_words
from arch.scope import (
    _is_source,
    _resolve_feature_dir,
    find_governed_files,
    find_source_files_under,
    scan_all_source,
    scan_all_text,
)
from arch.test_no_banned_words import BANNED_WORDS


def test_resolve_feature_dir_returns_dir_holding_arch_tests() -> None:
    got = _resolve_feature_dir("/x/app/foo/_arch_tests/test_a.py")
    assert got == Path("/x/app/foo").resolve()


def test_resolve_feature_dir_raises_outside_arch_tests() -> None:
    with pytest.raises(ValueError):
        _resolve_feature_dir("/x/app/foo/test_a.py")


@pytest.mark.parametrize(
    "relative, expected",
    [
        ("app/x.py", True),
        ("app/sub/y.py", True),
        ("tests/x.py", False),
        ("app/_arch_tests/test_x.py", False),
        (".venv/lib/x.py", False),
        ("_vendor/docetl/x.py", False),
        ("__pycache__/x.py", False),
    ],
)
def test_is_source(relative: str, expected: bool) -> None:
    assert _is_source(Path(relative)) is expected


def test_find_governed_files_scopes_to_folder_and_excludes_arch_tests(tmp_path: Path) -> None:
    feature = tmp_path / "app" / "widget"
    (feature / "_arch_tests").mkdir(parents=True)
    (feature / "_arch_tests" / "__init__.py").write_text("", encoding="utf-8")
    test_file = feature / "_arch_tests" / "test_rule.py"
    test_file.write_text("", encoding="utf-8")
    (feature / "code.py").write_text("x = 1\n", encoding="utf-8")
    (feature / "sub").mkdir()
    (feature / "sub" / "more.py").write_text("y = 2\n", encoding="utf-8")

    got = {p.name for p in find_governed_files(str(test_file))}
    assert got == {"code.py", "more.py"}


def test_scan_all_source_respects_repo_root_and_exemptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arch.scope as scope

    monkeypatch.setattr(scope, "_REPO_ROOT", tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "keep.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "skip.py").write_text("", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "skip.py").write_text("", encoding="utf-8")

    got = {p.name for p in scan_all_source()}
    assert got == {"keep.py"}


def test_find_governed_files_raises_when_scope_has_no_source(tmp_path: Path) -> None:
    # tests-first window: the _arch_tests/ folder exists before any sibling code
    feature = tmp_path / "app" / "newfeature"
    (feature / "_arch_tests").mkdir(parents=True)
    test_file = feature / "_arch_tests" / "test_rule.py"
    test_file.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="governs no source files"):
        find_governed_files(str(test_file))


def test_scan_all_source_raises_when_no_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arch.scope as scope

    monkeypatch.setattr(scope, "_REPO_ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "only.py").write_text("", encoding="utf-8")  # exempt -> nothing governable
    with pytest.raises(ValueError, match="no source files"):
        scope.scan_all_source()


def test_find_source_files_under_returns_single_file_target(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")
    assert find_source_files_under(target) == [target]


def test_find_source_files_under_walks_directory_excluding_exempt_parts(
    tmp_path: Path,
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "_arch_tests").mkdir()
    (tmp_path / "_arch_tests" / "c.py").write_text("", encoding="utf-8")
    assert {p.name for p in find_source_files_under(tmp_path)} == {"a.py"}


def test_find_source_files_under_raises_on_missing_file_target(tmp_path: Path) -> None:
    target = tmp_path / "gone.py"
    with pytest.raises(FileNotFoundError, match="missing path"):
        find_source_files_under(target)


def test_find_source_files_under_raises_on_missing_directory_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "no_such_dir"
    with pytest.raises(FileNotFoundError, match="missing path"):
        find_source_files_under(target)


def test_find_source_files_under_raises_when_directory_governs_nothing(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "only.py").write_text("", encoding="utf-8")  # exempt -> nothing governable
    with pytest.raises(ValueError, match="governs no source files"):
        find_source_files_under(tmp_path)


def test_scan_all_text_reads_what_git_would_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arch.scope as scope

    monkeypatch.setattr(scope, "_REPO_ROOT", tmp_path)
    banned_word = sorted(BANNED_WORDS)[0]
    _git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitignore").write_text("examples/\n", encoding="utf-8")
    for relative in ("docs/tracked.md", "docs/untracked.md", "examples/ignored.md"):
        (tmp_path / relative).parent.mkdir(exist_ok=True)
        (tmp_path / relative).write_text(f"the {banned_word} form\n", encoding="utf-8")
    _git(tmp_path, "add", "docs/tracked.md")

    offenders = find_banned_words(scan_all_text((".md",)), {banned_word}, exempt=set())
    assert [line.split("  ")[0] for line in offenders] == [
        f"{tmp_path}/docs/tracked.md:1",
        f"{tmp_path}/docs/untracked.md:1",
    ]


def test_scan_all_text_raises_outside_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arch.scope as scope

    monkeypatch.setattr(scope, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(RuntimeError, match="git ls-files` failed"):
        scan_all_text((".md",))


def test_scan_all_text_raises_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match="not on PATH"):
        scan_all_text((".md",))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True)
