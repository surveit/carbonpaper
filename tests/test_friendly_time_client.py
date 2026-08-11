"""app/static/friendly-time.js's `describeTime`, exercised in node.

The relative form is the only part with branching worth locking down; the DOM
sweep and the MutationObserver are the parts these tests do not reach.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_CLIENT = Path(__file__).resolve().parents[1] / "app" / "static" / "friendly-time.js"

# A fixed local wall-clock instant to measure every case against: Thursday
# 30 July 2026, 14:00. Local, not UTC — the module reads naive ISO stamps as
# browser-local time, so the test's "now" has to be local too.
_NOW_JS = "new Date(2026, 6, 30, 14, 0, 0)"


def _describe(iso: str, tmp_path: Path, *, relative: bool = True) -> str | None:
    probe = tmp_path / "probe.js"
    probe.write_text(
        f"const client = require({json.dumps(str(_CLIENT))});\n"
        f"console.log(JSON.stringify({{value: client.describeTime("
        f"{json.dumps(iso)}, {_NOW_JS}, {json.dumps(relative)})}}));\n",
        encoding="utf-8",
    )
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to exercise app/static/friendly-time.js")
    result = subprocess.run(
        [node, str(probe)], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise AssertionError(f"node exited {result.returncode}:\n{result.stderr}")
    value: str | None = json.loads(result.stdout)["value"]
    return value


def test_the_last_minute_reads_as_just_now(tmp_path):
    assert _describe("2026-07-30T13:59:30", tmp_path) == "just now"


def test_minutes_and_hours_count_up_from_there(tmp_path):
    assert _describe("2026-07-30T13:59:00", tmp_path) == "1 minute ago"
    assert _describe("2026-07-30T13:56:00", tmp_path) == "4 minutes ago"
    assert _describe("2026-07-30T13:00:00", tmp_path) == "1 hour ago"
    assert _describe("2026-07-30T12:00:00", tmp_path) == "2 hours ago"


def test_the_previous_calendar_day_reads_as_yesterday_with_the_clock_time(tmp_path):
    # Calendar-based, not 24-hour-based: 23:30 last night is still "yesterday".
    assert _describe("2026-07-29T16:12:00", tmp_path) == "yesterday, 4:12 PM"
    assert _describe("2026-07-29T23:30:00", tmp_path) == "yesterday, 11:30 PM"


def test_within_the_week_counts_days(tmp_path):
    assert _describe("2026-07-27T09:11:33", tmp_path) == "3 days ago"


def test_past_a_week_the_date_itself_is_more_use_than_a_count(tmp_path):
    older = _describe("2026-07-22T09:11:33", tmp_path)
    assert older == _describe("2026-07-22T09:11:33", tmp_path, relative=False)
    assert "ago" not in (older or "")
    assert "Jul 22" in (older or "")


def test_a_timestamp_ahead_of_the_clock_is_not_reported_as_ago(tmp_path):
    ahead = _describe("2026-07-30T15:00:00", tmp_path)
    assert "ago" not in (ahead or "")


def test_an_unparseable_datetime_yields_nothing_to_paint(tmp_path):
    assert _describe("not-a-date", tmp_path) is None
