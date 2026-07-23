from __future__ import annotations

from pathlib import Path

from arch import (
    check_no_fabricated_numbers,
    check_no_raw_disk,
    find_production_run_imports,
)


def test_check_no_raw_disk_flags_open_and_write(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def go(p):\n    open(p).read()\n    p.write_text('x')\n")
    offenders = check_no_raw_disk([bad])
    assert len(offenders) == 1
    assert "open" in offenders[0] and "write_text" in offenders[0]


def test_check_no_raw_disk_ignores_clean_file(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("def go():\n    return 1\n")
    assert check_no_raw_disk([ok]) == []


def test_check_no_raw_disk_flags_path_open_method(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def go(p):\n    with p.open() as f:\n        return f.read()\n")
    offenders = check_no_raw_disk([bad])
    assert len(offenders) == 1
    assert "open" in offenders[0]


def test_check_no_fabricated_numbers_flags_numeric_get(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def go(d):\n    return d.get('r', 1.0)\n")
    assert len(check_no_fabricated_numbers([bad])) == 1


def test_check_no_fabricated_numbers_respects_optout(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("def go(d):\n    return d.get('r', 1.0)  # data-default-ok: documented\n")
    assert check_no_fabricated_numbers([ok]) == []


def test_check_no_fabricated_numbers_ignores_bool_default(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("def go(d):\n    return d.get('flag', False)\n")
    assert check_no_fabricated_numbers([ok]) == []


def test_predicate_flags_production_run_import(tmp_path: Path) -> None:
    """find_production_run_imports flags app.runtime.runner (both `import` and
    `from` forms) but leaves the sanctioned app.runtime.executor surface alone."""
    from_form = tmp_path / "from_form.py"
    from_form.write_text("from app.runtime.runner import prepare_run\n")
    import_form = tmp_path / "import_form.py"
    import_form.write_text("import app.runtime.runner\n")
    clean = tmp_path / "clean.py"
    clean.write_text("from app.runtime.executor import run_subset\n")

    assert find_production_run_imports([from_form]) == [from_form.as_posix()]
    assert find_production_run_imports([import_form]) == [import_form.as_posix()]
    assert find_production_run_imports([clean]) == []
