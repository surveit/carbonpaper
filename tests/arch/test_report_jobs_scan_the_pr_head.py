"""A base-comparing report job must check out the PR head, not the default merge ref.

With no `ref`, actions/checkout gives a pull_request event refs/pull/N/merge — the branch
merged into master's tip — while the base checkout is the fork point. The difference is
every commit master gained since, printed as this PR's. All three reports shipped that way.
"""
from __future__ import annotations

from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
HEAD_SHA = "${{ github.event.pull_request.head.sha }}"
BASE_SHA = "${{ github.event.pull_request.base.sha }}"


def find_base_comparing_jobs() -> dict[str, list[dict]]:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    return {
        name: job["steps"]
        for name, job in jobs.items()
        if any(step.get("with", {}).get("ref") == BASE_SHA for step in job["steps"])
    }


def test_a_job_comparing_against_the_base_pins_its_head_checkout() -> None:
    unpinned = [
        name
        for name, steps in find_base_comparing_jobs().items()
        if not any(step.get("with", {}).get("ref") == HEAD_SHA for step in steps)
    ]
    assert not unpinned, (
        f"{unpinned} check out a base to diff against but take the default head checkout, "
        f"which is the merge ref. Pin it with `ref: {HEAD_SHA}` so the diff is the PR's own "
        "commits, and so the source links the report builds from head.sha resolve."
    )


def test_some_job_compares_against_the_base_so_the_rule_is_not_vacuous() -> None:
    assert find_base_comparing_jobs(), f"no job in {CI.name} checks out a base to compare against"
