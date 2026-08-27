"""The packet's README.md, written from the packet folder and nothing else."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.services.review_packet.readme import (
    MAX_TABLE_ROWS,
    README_FILE,
    write_packet_readme,
)

_HOME = "/Users/someone/Documents/lobbying-q1.csv"


@pytest.fixture
def packet(tmp_path):
    root = tmp_path / "proj-20260101T000000"
    _write_document(root, "# Who lobbied for Venezuela\nEvery filing, counted twice.\n")
    _write_workflow(root, _WORKFLOW)
    _write_manifest(root, _manifest())
    _write_csv(root, "q1_export", "filing_id\n1\n")
    _write_csv(root, "totals", "clients,spend,zip\n42,4461000.0,00501\n")
    return root


def _readme(root: Path) -> str:
    assert write_packet_readme(root) == README_FILE
    return (root / README_FILE).read_text(encoding="utf-8")


# ── What it says ─────────────────────────────────────────────────────────────


def test_it_opens_with_the_methodology_title_and_first_paragraph(packet):
    text = _readme(packet)
    assert text.startswith("# Who lobbied for Venezuela\n")
    assert "Every filing, counted twice." in text


def test_a_primary_claim_leads_with_its_value(packet):
    assert "### 42\n" in _readme(packet)


def test_a_secondary_claim_sits_in_a_table_beside_the_column_it_came_from(packet):
    row = _find_row(_readme(packet), "Total spend")
    assert "`4,461,000`" in row
    assert "`spend`" in row


def test_a_value_a_csv_may_have_retyped_is_printed_as_it_is_stored(packet):
    # `00501` is a zip code the CSV holds as text; `501` would be a different value.
    assert "`00501`" in _find_row(_readme(packet), "Filing zip")


def test_a_claim_whose_cell_is_missing_says_so(packet):
    (packet / "data" / "totals.csv").unlink()
    assert "unknown" in _find_row(_readme(packet), "Total spend")


# ── Sources ──────────────────────────────────────────────────────────────────


def test_a_source_is_named_by_its_basename_never_its_local_path(packet):
    text = _readme(packet)
    assert "`lobbying-q1.csv`" in text
    assert "/Users/" not in text, "a packet pushed to GitHub must not carry a home directory"


def test_a_source_links_the_copy_the_packet_holds(packet):
    _write_bytes(packet / "inputs" / "00-q1_export.csv", "filing_id\n1\n")
    assert "(inputs/00-q1_export.csv)" in _find_row(_readme(packet), "lobbying-q1.csv")


def test_a_source_with_no_copy_in_the_packet_is_named_but_not_linked(packet):
    assert _find_row(_readme(packet), "lobbying-q1.csv").startswith("| `lobbying-q1.csv` |")


def test_a_source_carries_the_whole_recorded_hash(packet):
    assert "`" + "ab" * 32 + "`" in _find_row(_readme(packet), "lobbying-q1.csv")


# ── How it was produced ──────────────────────────────────────────────────────


def test_the_steps_are_counted_by_what_ran_them(packet):
    text = _readme(packet)
    assert "**6 steps**: 1 called an AI model, 1 put a row to a person, 4 are plain code" in text


def test_it_states_the_run_the_version_and_the_status(packet):
    text = _readme(packet)
    assert "`20260101T000000`" in text
    assert "`v3`" in text
    assert "**ok**" in text


# ── Steps ────────────────────────────────────────────────────────────────────


def test_the_steps_run_one_branch_at_a_time(packet):
    assert _step_order(_readme(packet)) == [
        "q1_export", "stamp_q1", "q2_export", "stamp_q2", "both_quarters", "totals",
    ]


def test_a_joining_step_names_the_branches_it_joins(packet):
    row = _step_row(_readme(packet), "both_quarters")
    assert "`stamp_q1` + `stamp_q2`" in row


def test_a_step_with_one_input_names_no_join(packet):
    assert "+" not in _step_row(_readme(packet), "stamp_q1")


def test_a_step_the_packet_holds_no_csv_for_is_named_but_not_linked(packet):
    assert _step_row(_readme(packet), "stamp_q1").startswith("| `stamp_q1` |")


def test_a_step_links_the_csv_the_packet_does_hold(packet):
    assert "[`q1_export`](data/q1_export.csv)" in _readme(packet)


# ── What the run flagged ─────────────────────────────────────────────────────


def test_a_validation_issue_is_quoted_against_its_step(packet):
    row = _find_row(_readme(packet), "spend is null in 3 rows")
    assert "warning" in row and "`spend`" in row


def test_a_long_issue_is_cut_short_rather_than_dropped(packet):
    _write_manifest(packet, _manifest(issues=[_issue("verbose " * 100)]))
    row = _find_row(_readme(packet), "verbose")
    assert row.endswith("… |") and len(row) < 400


def test_a_capped_table_says_how_much_it_is_showing(packet):
    _write_manifest(packet, _manifest(issues=[_issue(f"issue {n}") for n in range(250)]))
    text = _readme(packet)
    assert f"The first {MAX_TABLE_ROWS} of 250 rows are shown" in text
    assert "manifest.json" in text


# ── The folder ───────────────────────────────────────────────────────────────


def test_the_folder_table_lists_only_what_is_here(packet):
    text = _readme(packet)
    assert "`manifest.json`" in text
    assert "`events.jsonl`" not in text, "this packet holds no event log"


def test_a_page_the_export_rendered_is_marked_as_not_authoritative(packet):
    _write_bytes(packet / "index.html", "<p>hi</p>")
    assert "A rendered view of the files above" in _find_row(_readme(packet), "index.html")


def test_the_two_files_written_after_this_page_are_still_listed(packet):
    text = _readme(packet)
    assert "`checksums.txt`" in text
    assert f"`{README_FILE}`" in text


# ── What it does without a workflow ──────────────────────────────────────────


def test_a_packet_whose_workflow_could_not_be_read_still_gets_a_readme(packet):
    (packet / "workflow.json").unlink()
    text = _readme(packet)
    assert "could not be read back" in text
    assert "## The steps" not in text
    assert "`lobbying-q1.csv`" in text, "the sources are still on record"


def test_a_packet_with_no_methodology_is_titled_by_its_project(packet):
    (packet / "methodology.md").unlink()
    assert _readme(packet).startswith("# proj\n")


# ── Fixture data ─────────────────────────────────────────────────────────────


def _find_row(text: str, needle: str) -> str:
    matches = [line for line in text.splitlines() if needle in line]
    assert matches, f"no line holding {needle!r}"
    return "\n".join(matches)


def _step_row(text: str, stage_id: str) -> str:
    return _find_row(text, f"`{stage_id}` |")


def _step_order(text: str) -> list[str]:
    body = text.split("## The steps", 1)[1].split("## What is in", 1)[0]
    return re.findall(r"^\| \[?`([^`]+)`", body, re.MULTILINE)


def _stage(stage_id, stage_type, inputs=(), outputs=()):
    return {
        "id": stage_id,
        "type": stage_type,
        "description": f"{stage_id} does its work",
        "inputs": [{"id": i, "schema": {"columns": []}} for i in inputs],
        "workflow_outputs": list(outputs) or None,
    }


_WORKFLOW = [
    _stage("q1_export", "input_data"),
    _stage("q2_export", "input_data"),
    _stage("stamp_q1", "python_row_function", ["q1_export"]),
    _stage("stamp_q2", "llm_transform", ["q2_export"]),
    _stage("both_quarters", "human_review_queue", ["stamp_q1", "stamp_q2"]),
    _stage(
        "totals",
        "aggregate",
        ["both_quarters"],
        [
            {"slug": "clients", "label": "Clients", "column": "clients", "primary": True},
            {"slug": "spend", "label": "Total spend", "column": "spend"},
            {"slug": "zip", "label": "Filing zip", "column": "zip"},
        ],
    ),
]


def _issue(message, column="spend"):
    return {"severity": "warning", "column": column, "message": message}


def _record(stage_id, rows, issues=()):
    return {
        "stage_id": stage_id,
        "type": "aggregate",
        "status": "ok",
        "output_row_count": rows,
        "output_path": f"outputs/{stage_id}.parquet",
        "input_validation_report": [],
        "output_validation_report": {"stage_id": stage_id, "ok": True, "issues": list(issues)},
    }


def _manifest(issues=(_issue("spend is null in 3 rows"),)):
    return {
        "project": "proj",
        "run_id": "20260101T000000",
        "status": "ok",
        "started_at": "2026-01-01T00:00:00",
        "finished_at": "2026-01-01T00:04:00",
        "workflow_version": "v3",
        "human_review_queue_stats": {},
        "input_bindings": {
            "q1_export": {
                "files": [{"path": _HOME, "sha256": "ab" * 32, "bytes": 2048}],
                "source": "upload",
            }
        },
        "stage_records": [
            _record("q1_export", 12),
            _record("totals", 1, issues),
        ],
    }


def _write_document(root: Path, text: str) -> None:
    _write_bytes(root / "methodology.md", text)


def _write_workflow(root: Path, stages: list[dict]) -> None:
    _write_bytes(root / "workflow.json", json.dumps(stages, indent=2))


def _write_manifest(root: Path, manifest: dict) -> None:
    _write_bytes(root / "manifest.json", json.dumps(manifest, indent=2))


def _write_csv(root: Path, stage_id: str, text: str) -> None:
    _write_bytes(root / "data" / f"{stage_id}.csv", text)


def _write_bytes(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
