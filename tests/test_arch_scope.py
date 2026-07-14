from __future__ import annotations

from pathlib import Path

import pytest

from arch.scope import (
    _is_source,
    _resolve_feature_dir,
    find_governed_files,
    scan_all_source,
)


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
    (feature / "_arch_tests" / "__init__.py").write_text("")
    test_file = feature / "_arch_tests" / "test_rule.py"
    test_file.write_text("")
    (feature / "code.py").write_text("x = 1\n")
    (feature / "sub").mkdir()
    (feature / "sub" / "more.py").write_text("y = 2\n")

    got = {p.name for p in find_governed_files(str(test_file))}
    assert got == {"code.py", "more.py"}


def test_scan_all_source_respects_repo_root_and_exemptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arch.scope as scope

    monkeypatch.setattr(scope, "_REPO_ROOT", tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "keep.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "skip.py").write_text("")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "skip.py").write_text("")

    got = {p.name for p in scan_all_source()}
    assert got == {"keep.py"}
