from __future__ import annotations

from pathlib import Path

from arch import (
    check_no_fabricated_numbers,
    check_no_raw_disk,
    find_inline_json_disk_reads,
    find_production_run_imports,
)


def test_check_no_raw_disk_flags_open_and_write(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def go(p):\n    open(p).read()\n    p.write_text('x')\n", encoding="utf-8")
    offenders = check_no_raw_disk([bad])
    assert len(offenders) == 1
    assert "open" in offenders[0] and "write_text" in offenders[0]


def test_check_no_raw_disk_ignores_clean_file(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("def go():\n    return 1\n", encoding="utf-8")
    assert check_no_raw_disk([ok]) == []


def test_check_no_raw_disk_flags_path_open_method(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def go(p):\n    with p.open() as f:\n        return f.read()\n", encoding="utf-8")
    offenders = check_no_raw_disk([bad])
    assert len(offenders) == 1
    assert "open" in offenders[0]


def test_check_no_fabricated_numbers_flags_numeric_get(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def go(d):\n    return d.get('r', 1.0)\n", encoding="utf-8")
    assert len(check_no_fabricated_numbers([bad])) == 1


def test_check_no_fabricated_numbers_respects_optout(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("def go(d):\n    return d.get('r', 1.0)  # data-default-ok: documented\n", encoding="utf-8")
    assert check_no_fabricated_numbers([ok]) == []


def test_check_no_fabricated_numbers_ignores_bool_default(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("def go(d):\n    return d.get('flag', False)\n", encoding="utf-8")
    assert check_no_fabricated_numbers([ok]) == []


def test_predicate_flags_production_run_import(tmp_path: Path) -> None:
    """find_production_run_imports flags app.runtime.runner (both `import` and
    `from` forms) but leaves the sanctioned app.runtime.executor surface alone."""
    from_form = tmp_path / "from_form.py"
    from_form.write_text("from app.runtime.runner import prepare_run\n", encoding="utf-8")
    import_form = tmp_path / "import_form.py"
    import_form.write_text("import app.runtime.runner\n", encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("from app.runtime.executor import run_subset\n", encoding="utf-8")

    assert find_production_run_imports([from_form]) == [from_form.as_posix()]
    assert find_production_run_imports([import_form]) == [import_form.as_posix()]
    assert find_production_run_imports([clean]) == []


def test_find_inline_json_disk_reads_flags_loads_of_read_text(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import json\ndef go(p):\n    return json.loads(p.read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    offenders = find_inline_json_disk_reads([bad], allow=set())
    assert len(offenders) == 1 and offenders[0].startswith(bad.as_posix())


def test_find_inline_json_disk_reads_flags_load_of_open(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import json\ndef go(p):\n    return json.load(open(p))\n", encoding="utf-8")
    assert len(find_inline_json_disk_reads([bad], allow=set())) == 1


def test_find_inline_json_disk_reads_flags_load_of_path_open_method(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import json\ndef go(p):\n    return json.load(p.open())\n", encoding="utf-8")
    assert len(find_inline_json_disk_reads([bad], allow=set())) == 1


def test_find_inline_json_disk_reads_respects_allow(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import json\ndef go(p):\n    return json.loads(p.read_text())\n", encoding="utf-8"
    )
    assert find_inline_json_disk_reads([bad], allow={"bad.py"}) == []


def test_find_inline_json_disk_reads_misses_a_read_staged_through_a_variable(
    tmp_path: Path,
) -> None:
    """Documents a known gap: no data-flow tracking across an assignment."""
    dodges = tmp_path / "dodges.py"
    dodges.write_text(
        "import json\ndef go(p):\n    text = p.read_text()\n    return json.loads(text)\n",
        encoding="utf-8",
    )
    assert find_inline_json_disk_reads([dodges], allow=set()) == []


def test_find_inline_json_disk_reads_ignores_json_loads_of_a_plain_string(
    tmp_path: Path,
) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("import json\ndef go(row):\n    return json.loads(row[0])\n", encoding="utf-8")
    assert find_inline_json_disk_reads([ok], allow=set()) == []
