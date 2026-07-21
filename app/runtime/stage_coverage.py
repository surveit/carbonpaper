"""Branch-coverage meter for a stage's authored tests.

`app.runtime.stage_tests` answers "do the tests pass?"; this module answers the
next question a reviewer asks: "do the tests actually witness the code?" A
green suite over a function with an untested `if/else` arm is a false sense of
safety — the reviewable claim this module produces is "N tests, X% branch
coverage", plus exactly which lines/branches no test ever reached, so a
reviewer can drill into what's unproven instead of trusting the pass count
alone.

Measurement runs `run_stage_tests` (the SAME execution path stage_tests.py
uses — fidelity again comes from sharing the path, not reimplementing it)
inside a `coverage.py` (branch=True) bracket scoped to just the transform's
own source lines, so a module-kind stage sharing a file with unrelated helpers
isn't penalized or flattered by code the stage doesn't own:

  module-kind function — the module already backs a real file on disk;
  measured directly.
  inline-kind function  — `exec()`-ing a bare string leaves coverage.py with
  no file to read source/branches back from, so the code is first written to
  a throwaway real .py file and imported as a "shadow" module-kind stage
  (same code, same callable, now file-backed) purely for this measurement.

This is a standalone capability — no caller wires it into the version gate or
generation-time gating here (that's issue #149's slice); it just answers "what
is this stage's branch coverage?" for whatever calls it.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import coverage

from app.core.models import FunctionKind, PythonFunction, Stage
from app.core.models.stages.stage_tests import STAGE_TEST_TYPES
from app.runtime.stage_tests import run_stage_tests


@dataclass
class UncoveredBranch:
    """One branch line where at least one exit was never taken (e.g. an `if`
    whose `else` no test reached). `branches_taken`/`branches_total` count exits
    FROM this line, not distinct outcomes — an `if` with no `else` still has 2
    exits (into the body, past it)."""
    line: int
    branches_taken: int
    branches_total: int


@dataclass
class StageTestCoverageReport:
    """The reviewable claim: `test_count` tests exercised `branch_percent`% of
    the transform's own branches. `uncovered_lines` are statements inside the
    function that no test ever reached at all; `uncovered_branches` are lines
    that ran but took only some of their possible exits — the drill-down a
    reviewer needs to write the missing case."""
    test_count: int
    branch_percent: float
    covered_branches: int
    total_branches: int
    uncovered_lines: list[int] = field(default_factory=list)
    uncovered_branches: list[UncoveredBranch] = field(default_factory=list)


def measure_stage_test_coverage(stage: Stage) -> StageTestCoverageReport:
    """Run `stage.tests` under branch-coverage measurement scoped to the
    stage's transform function. Raises ValueError for stage types with no
    runnable transform (mirrors run_stage_tests' guard), or if the transform's
    source can't be resolved to a real file (e.g. a module-kind stage pointing
    at a compiled/built-in module)."""
    if stage.type not in STAGE_TEST_TYPES:
        raise ValueError(
            f"stage {stage.id} ({stage.type}) has no transform function to measure coverage over"
        )
    with _measurable_stage(stage) as (measured_stage, source_path):
        fn = _resolve_function(measured_stage)
        start_line, end_line = _function_line_range(fn)

        cov = coverage.Coverage(branch=True, data_file=None, include=[str(source_path)], config_file=False)
        # A stage with no tests yet is a legitimate 0%-covered claim, not a
        # coverage.py misconfiguration — the warning coverage.py would
        # otherwise emit ("No data was collected") is noise here.
        cov.set_option("run:disable_warnings", ["no-data-collected"])
        cov.start()
        try:
            run_stage_tests(measured_stage)
        finally:
            cov.stop()

        branch_stats = cov.branch_stats(str(source_path))
        _, _, _, missing_lines, _ = cov.analysis2(str(source_path))

    return _build_report(
        test_count=len(stage.tests or []),
        branch_stats=branch_stats,
        missing_lines=missing_lines,
        start_line=start_line,
        end_line=end_line,
    )


def _build_report(
    *,
    test_count: int,
    branch_stats: dict[int, tuple[int, int]],
    missing_lines: list[int],
    start_line: int,
    end_line: int,
) -> StageTestCoverageReport:
    scoped = {
        line: counts for line, counts in branch_stats.items()
        if start_line <= line <= end_line
    }
    total_branches = sum(total for total, _taken in scoped.values())
    covered_branches = sum(taken for _total, taken in scoped.values())
    uncovered_branches = [
        UncoveredBranch(line=line, branches_taken=taken, branches_total=total)
        for line, (total, taken) in sorted(scoped.items())
        if taken < total
    ]
    uncovered_lines = sorted(
        # start_line itself is the `def ...:` line: it always "runs" once, at
        # import time, to create the function object — sometimes before
        # coverage.py starts collecting (a module-kind stage may already be
        # cached in sys.modules; a shadow module is built before its
        # coverage.Coverage() instance starts). Either way it carries no
        # branchable logic, so excluding it from the report can't hide a
        # real gap in the function's own behavior.
        line for line in missing_lines if start_line < line <= end_line
    )
    branch_percent = (
        100.0 if total_branches == 0 else round(100 * covered_branches / total_branches, 2)
    )
    return StageTestCoverageReport(
        test_count=test_count,
        branch_percent=branch_percent,
        covered_branches=covered_branches,
        total_branches=total_branches,
        uncovered_lines=uncovered_lines,
        uncovered_branches=uncovered_branches,
    )


def _resolve_function(stage: Stage) -> Callable[..., Any]:
    """The measured stage is always module-kind by the time this runs (inline
    code has been materialized into a shadow module) — mirrors
    python_functions._load_python_function's module branch."""
    assert stage.function is not None and stage.function.kind == FunctionKind.module.value
    assert stage.function.module is not None
    module = importlib.import_module(stage.function.module)
    fn_name = stage.function.function or "transform"
    return getattr(module, fn_name)  # type: ignore[no-any-return]


def _function_line_range(fn: Callable[..., Any]) -> tuple[int, int]:
    """The [first, last] source line the function's own def spans — coverage
    results outside this range belong to other code the module happens to
    carry (imports, unrelated helpers, other stages' functions) and must not
    count for or against this stage's claim."""
    lines, start = inspect.getsourcelines(fn)
    return start, start + len(lines) - 1


@contextmanager
def _measurable_stage(stage: Stage) -> Iterator[tuple[Stage, Path]]:
    """Yield (stage, source_path) where `stage`'s transform is guaranteed
    file-backed: a module-kind stage is passed through untouched; an
    inline-kind stage is materialized to a temp .py file and swapped for an
    equivalent module-kind stage for the duration of the `with` block."""
    assert stage.function is not None  # STAGE_TEST_TYPES membership guarantees this
    if stage.function.kind == FunctionKind.module.value:
        module = importlib.import_module(stage.function.module)  # type: ignore[arg-type]
        source_file = inspect.getsourcefile(module)
        if source_file is None:
            raise ValueError(
                f"stage {stage.id}: no source file for module {stage.function.module!r} "
                "(compiled/built-in modules can't be branch-measured)"
            )
        yield stage, Path(source_file)
        return
    with _shadow_module_stage(stage) as shadowed:
        yield shadowed


@contextmanager
def _shadow_module_stage(stage: Stage) -> Iterator[tuple[Stage, Path]]:
    """Write an inline-kind stage's code to a real file and import it as a
    throwaway module, so coverage.py has a file to read source/branches back
    from. Torn down on exit — the module never outlives this measurement."""
    assert stage.function is not None and stage.function.code is not None
    tmp_dir = Path(tempfile.mkdtemp(prefix="stage_coverage_"))
    module_name = f"stage_coverage_{uuid.uuid4().hex}"
    source_path = tmp_dir / f"{module_name}.py"
    try:
        source_path.write_text(stage.function.code)
        spec = importlib.util.spec_from_file_location(module_name, source_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"stage {stage.id}: could not import its inline code for measurement")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        shadow_function = PythonFunction.model_validate({
            "kind": "module",
            "module": module_name,
            "function": stage.function.function,
            "requirements": stage.function.requirements,
        })
        shadow_stage = stage.model_copy(update={"function": shadow_function})
        yield shadow_stage, source_path
    finally:
        sys.modules.pop(module_name, None)
        shutil.rmtree(tmp_dir)
