"""Generation-time stage-test pipeline: derive tests for every python transform of a
freshly-generated workflow, hold the code to them, and repair the code until they pass.

This is the point of the stage-test feature — the agent writing the tests — wired to run
right after `_finish_workflow` persists a generated workflow. It lives in app.web because it
is the one seam that must touch BOTH the compiler-built agents (the deriver / repair agents,
reached through the app.services.stage_tests hop since app.web may not import app.compiler) AND
the runtime (`run_stage_tests`, which executes a stage's real code) — a combination no lower
layer is allowed to hold, so the web layer owns the orchestration and hands it back to the
generation service as an injected coroutine (`start_workflow_generation`'s `on_persisted`).

Per python transform stage, in order:
  1. DERIVE — a headless, code-blind `Agent` (build_stage_test_deriver) authors a StageTest
     suite from the methodology + schemas alone, stamped `origin=generated`.
  2. RUN — execute the suite against the stage's actual code via run_stage_tests.
  3. REPAIR — while red, a code-only repair `Agent` rewrites the function (it has no
     test-editing lever) from the failure diffs; up to MAX_REPAIR_ATTEMPTS attempts.
  4. FAIL LOUD — a stage still red after the budget raises GenerationError rather than
     delivering a red stage as a reviewable artifact.

Stages whose tests are frozen (`stage_tests_are_frozen` — a human hand-authored or edited set,
carried across a regenerate by `_finish_workflow`) are skipped entirely: neither re-derived
nor overwritten. The design decision to run these agents HEADLESSLY (rather than as watchable
live chat turns) is deliberate — a per-stage derivation is a quiet background step of workflow
generation, consistent with generation "failing loudly" only when tests stay red.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.errors import GenerationError
from app.core.models import Stage
from app.core.models.stages.stage_tests import (
    GENERATED_ORIGIN,
    STAGE_TEST_TYPES,
    stage_tests_are_frozen,
)
from app.runtime.stage_tests import StageTestResult, run_stage_tests
from app.services.loader import load_workflow
from app.services.stage_edit import patch_stage_spec
from app.services.stage_tests import (
    build_stage_test_deriver,
    build_stage_test_repair_agent,
)

_log = logging.getLogger(__name__)

# The at-most-N code-repair attempts a red stage gets before generation fails loudly.
MAX_REPAIR_ATTEMPTS = 3


async def generate_stage_tests_for_workflow(
    project_dir: Path, *, document: str, model: str
) -> None:
    """Derive-and-repair tests for every python transform of the workflow now on disk.

    Iterates the freshly-persisted stages; for each python_row_function / python_frame_function
    with an output schema and no frozen (human) tests, derives a suite, runs it, and repairs the
    code until green. Raises GenerationError the moment any stage cannot be made green within the
    repair budget — generation fails loudly rather than leaving a red stage behind. Must be
    awaited on the server event loop (it drives headless agents that spawn CLI subprocesses)."""
    for stage in load_workflow(project_dir):
        if stage.type not in STAGE_TEST_TYPES:
            continue
        if stage.output_schema is None:
            # A python transform may validly lack an output schema, but a test needs one to
            # state expected rows — there is nothing to derive, so skip rather than raise.
            _log.info("stage %s has no output schema; skipping test derivation", stage.id)
            continue
        if stage_tests_are_frozen(stage.tests):
            _log.info("stage %s has human-frozen tests; leaving them untouched", stage.id)
            continue
        await _derive_run_and_repair(project_dir, stage.id, document=document, model=model)


async def _derive_run_and_repair(
    project_dir: Path, stage_id: str, *, document: str, model: str
) -> None:
    """Derive `stage_id`'s tests, run them, and repair its code until they pass — or raise."""
    stage = _reload(project_dir, stage_id)
    suite = await build_stage_test_deriver(document, stage, model=model).run()
    _write_generated_tests(project_dir, stage_id, suite.tests)

    stage = _reload(project_dir, stage_id)
    results = run_stage_tests(stage)
    if _all_passed(results):
        return
    await _repair_until_green(project_dir, stage_id, results, model=model)


def _write_generated_tests(project_dir: Path, stage_id: str, tests: list) -> None:
    """Stamp `origin=generated` on the derived cases and write them onto the stage, replacing
    any prior tests wholesale. An empty suite is a failure, not a silent test-wipe: it raises."""
    if not tests:
        raise GenerationError(
            f"stage-test derivation for '{stage_id}' in {project_dir.name} produced no cases"
        )
    stamped = [test.model_copy(update={"origin": GENERATED_ORIGIN}) for test in tests]
    patch = {"tests": [
        test.model_dump(mode="json", by_alias=True, exclude_none=True) for test in stamped
    ]}
    result = patch_stage_spec(project_dir, stage_id, json.dumps(patch))
    if not result.ok:
        raise GenerationError(
            f"derived tests for '{stage_id}' in {project_dir.name} did not validate: "
            + "; ".join(result.issues)
        )


async def _repair_until_green(
    project_dir: Path, stage_id: str, results: list[StageTestResult], *, model: str
) -> None:
    """Rewrite the stage's code until its (fixed) tests pass, or raise after the budget.

    Each attempt: a repair Agent proposes a whole replacement function from the current code and
    the latest failure report, the patch is applied (code only — never the tests), and the tests
    re-run. A rejected patch (invalid code) is fed back so the next attempt corrects it. A stage
    whose function is not inline has no code to rewrite here — that is an immediate loud failure."""
    stage = _reload(project_dir, stage_id)
    if stage.function is None or stage.function.code is None:
        raise GenerationError(
            f"stage '{stage_id}' in {project_dir.name} fails its tests but has no inline code "
            "to repair"
        )
    feedback = _render_failures(results)
    for _attempt in range(MAX_REPAIR_ATTEMPTS):
        stage = _reload(project_dir, stage_id)
        repaired = await build_stage_test_repair_agent(stage, feedback, model=model).run()
        patch = patch_stage_spec(
            project_dir, stage_id, json.dumps({"function": {"code": repaired.code}})
        )
        if not patch.ok:
            feedback = (
                "Your previous code was rejected before it could run:\n"
                + "\n".join(f"- {issue}" for issue in patch.issues)
                + "\nRewrite the function so it is valid, then satisfies the failing tests."
            )
            continue
        results = run_stage_tests(_reload(project_dir, stage_id))
        if _all_passed(results):
            return
        feedback = _render_failures(results)
    raise GenerationError(
        f"stage '{stage_id}' in {project_dir.name} still fails its tests after "
        f"{MAX_REPAIR_ATTEMPTS} repair attempt(s):\n{feedback}"
    )


def _reload(project_dir: Path, stage_id: str) -> Stage:
    """The stage as it currently sits on disk. Raises GenerationError if it vanished (a
    regenerate that dropped it out from under the pipeline) — never a silent skip."""
    stage = next((s for s in load_workflow(project_dir) if s.id == stage_id), None)
    if stage is None:
        raise GenerationError(f"stage '{stage_id}' vanished from {project_dir.name} mid-generation")
    return stage


def _all_passed(results: list[StageTestResult]) -> bool:
    return all(result.status == "passed" for result in results)


def _render_failures(results: list[StageTestResult]) -> str:
    """A human/agent-readable report of every non-passing result: its status, message, and each
    differing cell (expected vs actual). The repair agent works from this alone — it sees what
    the tests demand, but has no way to change what they demand."""
    lines: list[str] = []
    for result in results:
        if result.status == "passed":
            continue
        lines.append(f"- test {result.name!r}: {result.status}")
        if result.message:
            lines.append(f"    {result.message}")
        for diff in result.diffs:
            lines.append(
                f"    row {diff.row} column {diff.column!r}: "
                f"expected {diff.expected!r}, got {diff.actual!r}"
            )
    return "\n".join(lines)
