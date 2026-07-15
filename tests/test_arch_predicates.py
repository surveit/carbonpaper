from __future__ import annotations

from pathlib import Path

from arch import check_literal_confined, check_no_fabricated_numbers, check_no_raw_disk


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


def test_literal_confined_flags_non_owner(tmp_path: Path) -> None:
    owner = tmp_path / "app" / "services" / "drafts.py"
    owner.parent.mkdir(parents=True)
    owner.write_text('P = Path("drafts")\n', encoding="utf-8")
    other = tmp_path / "app" / "other.py"
    other.write_text('D = Path("drafts")\n', encoding="utf-8")
    offenders = check_literal_confined(
        [owner, other], literal="drafts", owner_suffix="app/services/drafts.py"
    )
    assert len(offenders) == 1 and "other.py" in offenders[0]


def test_literal_confined_ignores_docstrings_and_substrings(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text(
        '"""Talks about drafts in prose."""\n'
        'LABEL = "Saving the draft"\n'
        'X = "the drafts folder"\n',
        encoding="utf-8",
    )
    assert check_literal_confined(
        [clean], literal="drafts", owner_suffix="app/services/drafts.py"
    ) == []
