"""Records which architecture gates failed on a CI run, and renders the running tally.

Usage:
    python scripts/arch_failure_ledger.py record --junit X.xml --imports Y.txt > record.json
    python scripts/arch_failure_ledger.py render ledger.jsonl > body.md

`record` reads the two artifacts a failing CI run leaves behind — pytest's JUnit XML and
the captured stdout of `lint-imports` — and emits one JSON line naming every architecture
gate that failed. `render` folds a whole ledger of those lines into the markdown body of
the tally issue.

A run whose gates all passed still emits a record, with an empty `findings`: the
denominator is the point, and a ledger of failures alone cannot say how often a gate held.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ElementTree
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

_MARKER = "<!-- arch-failure-ledger -->"
_ARCH_TEST_FILE = re.compile(r"^(tests/arch/|app/(?:[a-z_0-9]+/)*_arch_tests/)")
_BROKEN_CONTRACT = re.compile(r"^(?P<name>.+?) BROKEN$")
_CONTRACT_TALLY = re.compile(r"Contracts: (?P<kept>\d+) kept, (?P<broken>\d+) broken")
_RECENT_DAYS = 30


class Finding(BaseModel):
    gate: str
    name: str
    case: str = ""


class RunRecord(BaseModel):
    run_id: str
    run_url: str
    created: str
    branch: str
    event: str
    contracts_kept: int | None = None
    findings: list[Finding] = []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    recorder = sub.add_parser("record")
    recorder.add_argument("--junit", type=Path)
    recorder.add_argument("--imports", type=Path)
    recorder.add_argument("--run-id", default="")
    recorder.add_argument("--run-url", default="")
    recorder.add_argument("--created", default="")
    recorder.add_argument("--branch", default="")
    recorder.add_argument("--event", default="")

    renderer = sub.add_parser("render")
    renderer.add_argument("ledger", type=Path)

    args = parser.parse_args()
    if args.command == "record":
        print(build_record(args).model_dump_json())
    else:
        print(render_markdown(read_ledger(args.ledger)))
    return 0


def build_record(args: argparse.Namespace) -> RunRecord:
    findings = list(find_failed_arch_tests(args.junit)) + list(find_broken_contracts(args.imports))
    return RunRecord(
        run_id=args.run_id,
        run_url=args.run_url,
        created=args.created,
        branch=args.branch,
        event=args.event,
        contracts_kept=read_contracts_kept(args.imports),
        findings=findings,
    )


def find_failed_arch_tests(junit_path: Path | None):
    """Yield a Finding per failing testcase whose file is an architecture test."""
    if junit_path is None or not junit_path.exists():
        return
    for case in ElementTree.parse(junit_path).getroot().iter("testcase"):
        if not list(case.iter("failure")) and not list(case.iter("error")):
            continue
        source = case.get("file") or module_path_of(case.get("classname") or "")
        if not _ARCH_TEST_FILE.match(source):
            continue
        yield Finding(gate="arch_test", name=source, case=case.get("name") or "")


def module_path_of(classname: str) -> str:
    """`tests.arch.test_x.TestCase` -> `tests/arch/test_x.py`.

    Only the xunit2 junit family sets a `file` attribute; the default emits this dotted
    form, and a testcase inside a class appends the class name to its module.
    """
    segments = classname.split(".")
    while segments and segments[-1][:1].isupper():
        segments.pop()
    return "/".join(segments) + ".py" if segments else ""


def find_broken_contracts(imports_path: Path | None):
    """Yield a Finding per import-linter contract the run reported as BROKEN."""
    if imports_path is None or not imports_path.exists():
        return
    text = imports_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        match = _BROKEN_CONTRACT.match(line.strip())
        if match:
            yield Finding(gate="contract", name=match.group("name").strip())


def read_contracts_kept(imports_path: Path | None) -> int | None:
    if imports_path is None or not imports_path.exists():
        return None
    text = imports_path.read_text(encoding="utf-8", errors="replace")
    match = _CONTRACT_TALLY.search(text)
    return int(match.group("kept")) if match else None


def read_ledger(path: Path) -> list[RunRecord]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [RunRecord.model_validate_json(line) for line in text.splitlines() if line.strip()]


def render_markdown(raw_records: list[RunRecord]) -> str:
    records = merge_by_run(raw_records)
    recent = select_recent(records)
    sections = [
        _MARKER,
        "# Architecture gate tally",
        "",
        f"{len(records)} CI runs recorded. Rewritten by the `lint` and `test` jobs on every "
        "run; raw records live on the `ci-metrics` branch as `ledger.jsonl`.",
        "",
        render_headline(records, recent),
        "",
        render_gate_table("Arch tests", "arch_test", records, recent),
        "",
        render_gate_table("Import-linter contracts", "contract", records, recent),
        "",
        render_note(records),
    ]
    return "\n".join(sections)


def render_headline(records: list[RunRecord], recent: list[RunRecord]) -> str:
    def clean_share(subset: list[RunRecord]) -> str:
        if not subset:
            return "—"
        clean = sum(1 for record in subset if not record.findings)
        return f"{round(100 * clean / len(subset))}%"

    rows = [
        "| | All time | Last 30 days |",
        "| --- | ---: | ---: |",
        f"| Runs recorded | {len(records)} | {len(recent)} |",
        f"| Runs with a gate failure | {count_failing(records)} | {count_failing(recent)} |",
        f"| Runs fully clean | {clean_share(records)} | {clean_share(recent)} |",
    ]
    return "\n".join(rows)


def render_gate_table(
    title: str, gate: str, records: list[RunRecord], recent: list[RunRecord]
) -> str:
    all_time = count_by_name(records, gate)
    last_30 = count_by_name(recent, gate)
    if not all_time:
        return f"## {title}\n\nNo failure recorded yet."
    rows = [f"## {title}", "", "| Gate | Failing runs (all time) | Last 30 days |",
            "| --- | ---: | ---: |"]
    rows += [
        f"| `{name}` | {count} | {last_30[name]} |"
        for name, count in all_time.most_common()
    ]
    return "\n".join(rows)


def render_note(records: list[RunRecord]) -> str:
    kept = next(
        (record.contracts_kept for record in reversed(records) if record.contracts_kept),
        None,
    )
    if kept is None:
        return ""
    return (
        f"---\n\n*A contract table with zeros is the expected result: {kept} contracts are "
        "checked on every run, and a broken one is normally caught and fixed before a push. "
        "This counts what reached CI, which is a lower bound on what the gates did.*"
    )


def merge_by_run(records: list[RunRecord]) -> list[RunRecord]:
    """One CI run writes one record per gate job, so fold them back into one run each.

    The `lint` and `test` jobs each append what their own gate saw. Counting the raw lines
    would report a run twice and halve every clean-run percentage.
    """
    merged: dict[str, RunRecord] = {}
    for record in records:
        held = merged.get(record.run_id)
        if held is None:
            merged[record.run_id] = record.model_copy(deep=True)
            continue
        held.findings.extend(record.findings)
        if held.contracts_kept is None:
            held.contracts_kept = record.contracts_kept
        held.created = held.created or record.created
    return list(merged.values())


def count_failing(records: list[RunRecord]) -> int:
    return sum(1 for record in records if record.findings)


def count_by_name(records: list[RunRecord], gate: str) -> Counter[str]:
    tally: Counter[str] = Counter()
    for record in records:
        for name in {f.name for f in record.findings if f.gate == gate}:
            tally[name] += 1
    return tally


def select_recent(records: list[RunRecord]) -> list[RunRecord]:
    cutoff = datetime.now().astimezone() - timedelta(days=_RECENT_DAYS)
    return [record for record in records if parse_created(record.created) >= cutoff]


def parse_created(created: str) -> datetime:
    """Missing or unparseable timestamps sort as ancient, never into the recent window."""
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)


if __name__ == "__main__":
    sys.exit(main())
