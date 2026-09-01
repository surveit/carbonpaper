"""app/web/figure_text.py and its mirror in app/static/figure_text.js."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.web.figure_text import GROUP_MARK, render_figure

_CLIENT = Path(__file__).resolve().parents[1] / "app" / "static" / "figure_text.js"

_CASES = [2026, 1002, 9999, 10000, 45154.03, 8263187740.090001, -1234567, 0]


def test_a_bool_is_not_a_figure():
    assert render_figure(True) == "True"
    assert render_figure(False) == "False"


@pytest.mark.parametrize("value", _CASES)
def test_grouping_keeps_every_digit(value):
    assert render_figure(value).replace(GROUP_MARK, "") == str(value)


def test_four_digits_stay_bare():
    assert render_figure(2026) == "2026"
    assert render_figure(10000) != "10000"


def test_the_mark_is_not_a_plain_space():
    """Else a grouped number would collide with string data in the row diff."""
    assert GROUP_MARK != " "
    assert render_figure("10 000") == "10 000"


@pytest.mark.parametrize("value", _CASES)
def test_the_browser_prints_what_the_server_prints(value, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to exercise app/static/figure_text.js")
    probe = tmp_path / "probe.js"
    probe.write_text(
        "global.window = {};\n"
        f"require({json.dumps(str(_CLIENT))});\n"
        f"console.log(JSON.stringify(window.Figures.text({json.dumps(value)})));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(probe)], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise AssertionError(f"node exited {result.returncode}:\n{result.stderr}")
    assert json.loads(result.stdout) == render_figure(value)
