"""The shared row reader: what one call may pull, where it starts, and what the
lineage link on each row resolves against."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import app.services.run as run_service
from app.services.project import save_working_copy_as_version
from app.services import workspace
from app.tools import shared
from stage_seed import add_stage

_PROJECT = "filings_review"
_ROWS = 60


def _make_project(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "filing_id": [f"F-{n:03d}" for n in range(_ROWS)],
        "amount_usd": list(range(_ROWS)),
    }).to_csv(root / "data" / "filings.csv", index=False)
    stage = {
        "id": "load", "description": "Load filings", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "filings.csv"),
                                 "format": "csv"}},
        "signature": {"form": "replaces", "produces": [
            {"name": "filing_id", "type": "str", "nullable": False},
            {"name": "amount_usd", "type": "int", "nullable": False},
        ]},
    }
    add_stage(root, stage)
    save_working_copy_as_version(root.name, message="seed")
    return str(run_service.execute(_PROJECT)["run_id"])


@pytest.fixture
def run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return _make_project(tmp_path / _PROJECT)


def _read(run: str, limit: int | None = None, offset: int = 0) -> shared.StageOutputRows:
    return shared.read_stage_output_rows(_PROJECT, run, "load", limit, offset)


def test_one_call_reads_at_most_the_cap_and_says_where_it_stopped(run_id: str) -> None:
    out = _read(run_id)

    assert [row.ordinal for row in out.rows] == list(range(shared.MAX_OUTPUT_ROWS))
    assert out.limit == shared.MAX_OUTPUT_ROWS
    # What the window is a window OF, so a caller can tell a page from the whole output.
    assert out.row_count == _ROWS


def test_asking_for_more_than_the_cap_reports_the_limit_it_applied(run_id: str) -> None:
    """Clamped, not refused — and never silently, or 50 of 200 rows reads as all of them."""
    out = _read(run_id, limit=200)

    assert len(out.rows) == shared.MAX_OUTPUT_ROWS
    assert out.limit == shared.MAX_OUTPUT_ROWS


def test_offset_names_the_ordinal_the_window_starts_at(run_id: str) -> None:
    out = _read(run_id, limit=5, offset=55)

    assert out.offset == 55
    assert [row.ordinal for row in out.rows] == [55, 56, 57, 58, 59]
    assert [row.values["filing_id"] for row in out.rows] == [
        f"F-{n:03d}" for n in range(55, 60)
    ]


def test_an_ordinal_is_the_row_s_place_in_the_stage_output_not_in_the_window(
    run_id: str,
) -> None:
    """The lineage link is built from it, so a window-relative ordinal would link elsewhere."""
    out = _read(run_id, limit=3, offset=10)

    assert out.rows[0].lineage_url.endswith("/stage/load/row/10/trace/view")
    assert out.rows[2].lineage_url.endswith("/stage/load/row/12/trace/view")


def test_a_window_past_the_end_reads_no_rows_and_still_reports_the_count(
    run_id: str,
) -> None:
    out = _read(run_id, offset=_ROWS + 10)

    assert out.rows == []
    assert out.row_count == _ROWS


@pytest.mark.parametrize("limit,offset", [(0, 0), (-1, 0), (None, -1)])
def test_a_window_that_cannot_be_read_is_refused(
    run_id: str, limit: int | None, offset: int
) -> None:
    with pytest.raises(ValueError, match="limit must be at least 1"):
        _read(run_id, limit, offset)


def test_the_link_is_root_relative_until_a_caller_supplies_its_own_base(
    run_id: str,
) -> None:
    """The MCP surface has no base URL; a chat whose reader clicks the link passes one."""
    bare = _read(run_id, limit=1).rows[0].lineage_url
    based = shared.read_stage_output_rows(
        _PROJECT, run_id, "load", 1, base_url="http://127.0.0.1:8765"
    ).rows[0].lineage_url

    assert bare == f"/project/{_PROJECT}/runs/{run_id}/stage/load/row/0/trace/view"
    assert based == f"http://127.0.0.1:8765{bare}"
