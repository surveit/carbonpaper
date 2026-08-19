"""A real SIGKILL mid-stage, then a re-run against the same on-disk store.

The in-process failure tests stage exceptions; this stages the machine going
away. What the second process has to re-call is what the kill destroyed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_CHILD = Path(__file__).parent / "llm_crash_child.py"
_REPO = Path(__file__).resolve().parents[2]
_ROWS = 8


def _spawn(mode: str, batch_size: int, kill_after: int, db: Path, probe: Path) -> int:
    result = subprocess.run(
        [sys.executable, str(_CHILD), mode, str(batch_size), str(kill_after)],
        cwd=_REPO, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": f"{_REPO}:{_CHILD.parent}",
             "CARBON_PAPER_DB_PATH": str(db), "CRASH_PROBE": str(probe)})
    if mode == "replay":
        assert result.returncode == 0, result.stderr
    return result.returncode


def _calls(probe: Path, mode: str) -> int:
    if not probe.exists():
        return 0
    return sum(1 for line in probe.read_text(encoding="utf-8").splitlines() if line == mode)


@pytest.mark.parametrize("batch_size,kill_after", [(1, 5), (2, 3)],
                         ids=["per-row", "batched"])
def test_what_a_sigkill_mid_stage_destroys(tmp_path, batch_size, kill_after):
    db, probe = tmp_path / "app.db", tmp_path / "probe.log"

    code = _spawn("kill", batch_size, kill_after, db, probe)
    assert code == -9, f"the child was meant to be SIGKILLed, exited {code}"
    assert _calls(probe, "kill") == kill_after

    _spawn("replay", batch_size, 0, db, probe)
    answered_before_the_kill = kill_after - 1
    recovered = _ROWS - _calls(probe, "replay") * batch_size
    print(f"\nbatch_size={batch_size}: {answered_before_the_kill} call(s) answered "
          f"before the kill, {recovered} row(s) survived it")
    assert recovered == (answered_before_the_kill * batch_size if batch_size == 1 else 0)
