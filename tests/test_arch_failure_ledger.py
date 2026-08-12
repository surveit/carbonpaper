"""Fixtures are real captured CI-gate output.

`junit_two_arch_failures.xml` is a trimmed run of this repo's own arch suite;
`lint_imports_one_broken.txt` came from temporarily importing `app.core.persistence`
from an `app.models` module. Invented output would agree with a format nothing emits.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.arch_failure_ledger import (
    Finding,
    RunRecord,
    find_broken_contracts,
    find_failed_arch_tests,
    module_path_of,
    read_contracts_kept,
    read_ledger,
    render_markdown,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "arch_gates"
_JUNIT = _FIXTURES / "junit_two_arch_failures.xml"
_IMPORTS_BROKEN = _FIXTURES / "lint_imports_one_broken.txt"
_IMPORTS_KEPT = _FIXTURES / "lint_imports_all_kept.txt"


def test_a_failing_arch_test_is_named_by_its_source_path() -> None:
    findings = list(find_failed_arch_tests(_JUNIT))
    assert [f.name for f in findings] == [
        "tests/arch/test_file_io_declares_encoding.py",
        "tests/arch/test_no_fabricated_numbers.py",
    ]
    assert findings[0].case == "test_no_text_file_io_without_an_explicit_encoding"
    assert {f.gate for f in findings} == {"arch_test"}


def test_a_passing_testcase_is_not_recorded() -> None:
    # The fixture holds three cases; only two carry a <failure>.
    assert len(list(find_failed_arch_tests(_JUNIT))) == 2


def test_a_failing_test_outside_the_arch_suite_is_ignored() -> None:
    assert module_path_of("tests.test_admin_ui") == "tests/test_admin_ui.py"
    assert not _matches_arch("tests/test_admin_ui.py")
    assert _matches_arch("tests/arch/test_no_dunder_all.py")
    assert _matches_arch("app/web/_arch_tests/test_web_names_projects_by_id.py")


def test_a_testcase_inside_a_class_still_resolves_to_its_module() -> None:
    assert module_path_of("tests.arch.test_import_graph.TestCycles") == (
        "tests/arch/test_import_graph.py"
    )


def test_a_broken_contract_is_named_by_its_contract_name() -> None:
    findings = list(find_broken_contracts(_IMPORTS_BROKEN))
    assert findings == [
        Finding(gate="contract", name="app.models stays pure — never imports the store")
    ]


def test_a_run_with_every_contract_kept_records_none() -> None:
    assert list(find_broken_contracts(_IMPORTS_KEPT)) == []
    assert read_contracts_kept(_IMPORTS_KEPT) == 14
    assert read_contracts_kept(_IMPORTS_BROKEN) == 13


def test_a_missing_artifact_records_nothing_rather_than_a_pass() -> None:
    absent = _FIXTURES / "no-such-file.txt"
    assert list(find_failed_arch_tests(absent)) == []
    assert list(find_broken_contracts(absent)) == []
    assert read_contracts_kept(absent) is None


def test_the_tally_counts_each_run_once_per_gate(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        "\n".join(record.model_dump_json() for record in _two_failing_and_one_clean_run()),
        encoding="utf-8",
    )
    body = render_markdown(read_ledger(ledger))
    assert "| Runs recorded | 3 |" in body
    assert "| Runs with a gate failure | 2 |" in body
    assert "| `tests/arch/test_no_fabricated_numbers.py` | 2 |" in body
    assert "| `app.models stays pure — never imports the store` | 1 |" in body


def test_a_gate_failing_twice_in_one_run_still_counts_that_run_once(tmp_path: Path) -> None:
    repeated = RunRecord(
        run_id="9", run_url="", created=_now(), branch="x", event="push",
        findings=[
            Finding(gate="arch_test", name="tests/arch/test_a.py", case="test_one"),
            Finding(gate="arch_test", name="tests/arch/test_a.py", case="test_two"),
        ],
    )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(repeated.model_dump_json(), encoding="utf-8")
    assert "| `tests/arch/test_a.py` | 1 |" in render_markdown(read_ledger(ledger))


def test_a_ledger_with_no_failure_says_so_rather_than_showing_an_empty_table(
    tmp_path: Path,
) -> None:
    clean = RunRecord(
        run_id="1", run_url="", created=_now(), branch="master", event="push",
        contracts_kept=14,
    )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(clean.model_dump_json(), encoding="utf-8")
    body = render_markdown(read_ledger(ledger))
    assert body.count("No failure recorded yet.") == 2
    assert "| Runs fully clean | 100% |" in body


@pytest.mark.parametrize("stamp", ["", "not-a-date"])
def test_an_unparseable_timestamp_never_lands_in_the_recent_window(
    tmp_path: Path, stamp: str
) -> None:
    record = RunRecord(
        run_id="1", run_url="", created=stamp, branch="master", event="push",
        findings=[Finding(gate="arch_test", name="tests/arch/test_a.py")],
    )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(record.model_dump_json(), encoding="utf-8")
    body = render_markdown(read_ledger(ledger))
    assert "| Runs recorded | 1 | 0 |" in body


# --- test helpers ------------------------------------------------------------


def _matches_arch(path: str) -> bool:
    from scripts.arch_failure_ledger import _ARCH_TEST_FILE

    return _ARCH_TEST_FILE.match(path) is not None


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat()


def _two_failing_and_one_clean_run() -> list[RunRecord]:
    return [
        RunRecord(
            run_id="1", run_url="", created=_now(), branch="a", event="pull_request",
            contracts_kept=14,
            findings=list(find_failed_arch_tests(_JUNIT)),
        ),
        RunRecord(
            run_id="2", run_url="", created=_now(), branch="b", event="pull_request",
            contracts_kept=13,
            findings=[
                Finding(gate="arch_test", name="tests/arch/test_no_fabricated_numbers.py"),
                *find_broken_contracts(_IMPORTS_BROKEN),
            ],
        ),
        RunRecord(
            run_id="3", run_url="", created=_now(), branch="master", event="push",
            contracts_kept=14,
        ),
    ]
