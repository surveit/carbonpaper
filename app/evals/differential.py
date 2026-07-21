"""N-version differential derivation: an ambiguity smoke detector for a stage's
frozen tests.

The stage-test gate (app.runtime.stage_tests) holds ONE implementation to a
frozen suite. But a suite that a correct implementation passes can still
UNDERDETERMINE the methodology: two faithful readings of the same prose can
both pass every authored case yet disagree on an input no case covers. That
disagreement is not a bug in either implementation — it is evidence the tests
(and the prose behind them) leave a behavior unpinned.

This module derives the transform for one stage N times in INDEPENDENT contexts
(each derivation is a fresh agent that never sees the others' code), keeps only
the candidates that pass every frozen test, and runs those survivors against
PROBE inputs drawn from beyond the frozen set. If two survivors diverge on a
probe, that divergence is surfaced as an `AmbiguityFinding` — a structured
adjudication question for a human, never resolved by majority vote (agreement
across same-model samples is weak evidence; this is a smoke detector, not a
proof). No divergence, or too few survivors to compare, is reported as a clean
result. All candidates failing is a bad spec or broken derivation, NOT
ambiguity, and is reported as its own status.

Standalone by design: `derive_n_version_and_diff` takes the derivation step as
an injectable callable (so it is mockable, and so a future generation-time gate
— issue #149 — can supply its own deriver) and returns the finding as data. It
does not persist the finding or feed it back into the methodology prose; that
flow is issue #153.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

from pydantic import BaseModel, model_validator

from app.core.agent.agent import Agent
from app.core.models import Stage, TableSchema
from app.core.models.stage import FunctionKind, PythonFunction
from app.core.models.stages.code import validate_inline_function_code
from app.core.models.stages.stage_tests import STAGE_TEST_TYPES
from app.runtime.stage_tests import TransformOutcome, outputs_agree, run_stage_tests, run_transform

# The default number of independent derivations. Small on purpose: this is a
# smoke detector, and each version is a full agent derivation. Three is enough
# to see two faithful readings pull apart without spending a run per candidate.
DEFAULT_N = 3

# A probe input bundle has the same shape as a StageTest's inputs: each declared
# upstream id mapped to its rows. Row cells are caller-defined dynamic JSON, the
# same genuine dynamic boundary StageTest.inputs/expected sit on.
InputBundle = dict[str, list[dict[str, Any]]]

Status = Literal["no_candidates_passed", "no_ambiguity", "ambiguity_detected"]


@dataclass
class DerivedCandidate:
    """One independently-derived transform for the stage under study: `index`
    is its derivation ordinal (0-based), `code` the inline python it defines,
    `function` the entry-point name (None = the runtime default `transform`)."""
    index: int
    code: str
    function: str | None = None


class DerivedTransformCode(BaseModel):
    """The submit_answer schema for the code deriver: the inline python for the
    stage's transform. Validated to compile and define its entry point, so a
    non-runnable submission bounces inside the agent loop rather than surfacing
    as a spurious 'diverging' candidate later."""
    code: str
    function: Optional[str] = None

    @model_validator(mode="after")
    def _code_is_runnable(self) -> "DerivedTransformCode":
        validate_inline_function_code(self.code, self.function)
        return self


class CandidateOutput(BaseModel):
    """One passing candidate's observable behavior on the diverging probe:
    `rows` is its output (projected onto the output schema), or None with
    `error` set when it raised or the probe was malformed for it."""
    candidate_index: int
    rows: Optional[list[dict[str, Any]]] = None
    error: Optional[str] = None


class DivergingProbe(BaseModel):
    """The probe input on which the surviving candidates disagreed, with each
    candidate's output on it — the concrete evidence of the underdetermination."""
    inputs: InputBundle
    outputs: list[CandidateOutput]


class AmbiguityFinding(BaseModel):
    """A detected underdetermination, framed as a question for a human to
    adjudicate: on `probe` — an input the frozen tests do not cover — candidates
    that ALL pass the frozen suite produced different behavior, so the tests (and
    the methodology behind them) leave this case unpinned. Surfaced as data; the
    resolution flow (feeding the adjudication back into the prose) is issue #153.
    Never carries a 'winner' — divergence is the finding, not a vote to break."""
    stage_id: str
    stage_name: str
    summary: str
    passing_candidate_count: int
    probe: DivergingProbe


class DifferentialReport(BaseModel):
    """The outcome of one N-version differential run. `finding` is present iff
    `status == "ambiguity_detected"`. `no_candidates_passed` means every
    derivation failed the frozen suite (a bad spec or broken derivation — NOT
    ambiguity); `no_ambiguity` means the survivors agreed on every probe (or
    there were too few survivors, or no probes, to compare)."""
    stage_id: str
    n: int
    passing_candidate_count: int
    failing_candidate_count: int
    probe_count: int
    status: Status
    finding: Optional[AmbiguityFinding] = None
    notes: list[str] = []


# The derivation step: given a candidate index, produce that candidate's code in
# an independent context. Injectable so callers can mock it (tests) or swap the
# backend (a future generation-time gate). Async because the default is an agent.
DeriveCandidate = Callable[[int], Awaitable[DerivedCandidate]]


async def derive_n_version_and_diff(
    document: str,
    stage: Stage,
    *,
    probe_inputs: list[InputBundle],
    n: int = DEFAULT_N,
    model: str = "sonnet",
    derive_candidate: DeriveCandidate | None = None,
) -> DifferentialReport:
    """Derive `stage`'s transform `n` times independently, keep the candidates
    that pass EVERY frozen test in `stage.tests`, and look for behavioral
    divergence among the survivors on `probe_inputs` (cases beyond the frozen
    set). Returns a DifferentialReport carrying an AmbiguityFinding when two
    survivors disagree on a probe, else a clean result.

    `derive_candidate(index)` produces the index-th candidate in a context blind
    to the others; the default runs a fresh code-deriver agent per index (the
    derivations run concurrently, so no candidate can see another's output).
    Raises ValueError unless the stage is a python transform with an output
    schema and a non-empty frozen suite (there is nothing to underdetermine
    without tests), and unless `n >= 2` (a differential needs at least two
    versions to differ)."""
    _require_differentiable(stage, n)
    derive = derive_candidate or _agent_deriver(document, stage, model=model)

    candidates = await asyncio.gather(*(derive(index) for index in range(n)))
    passing, failing_notes = _partition_by_frozen_tests(stage, candidates)

    if not passing:
        return DifferentialReport(
            stage_id=stage.id, n=n, passing_candidate_count=0,
            failing_candidate_count=len(candidates), probe_count=len(probe_inputs),
            status="no_candidates_passed",
            notes=["no candidate passed the frozen suite — a bad spec or broken "
                   "derivation, not ambiguity"] + failing_notes,
        )

    finding = _find_divergence(stage, passing, probe_inputs)
    if finding is None:
        notes = failing_notes
        if len(passing) < 2:
            notes = ["only one candidate passed the frozen suite — nothing to "
                     "differentiate against"] + notes
        elif not probe_inputs:
            notes = ["no probe inputs supplied — behavioral divergence beyond the "
                     "frozen tests cannot be observed"] + notes
        return DifferentialReport(
            stage_id=stage.id, n=n, passing_candidate_count=len(passing),
            failing_candidate_count=len(candidates) - len(passing),
            probe_count=len(probe_inputs), status="no_ambiguity", notes=notes,
        )

    return DifferentialReport(
        stage_id=stage.id, n=n, passing_candidate_count=len(passing),
        failing_candidate_count=len(candidates) - len(passing),
        probe_count=len(probe_inputs), status="ambiguity_detected",
        finding=finding, notes=failing_notes,
    )


def _require_differentiable(stage: Stage, n: int) -> None:
    if stage.type not in STAGE_TEST_TYPES:
        raise ValueError(
            f"stage {stage.id} ({stage.type}) is not a python transform; N-version "
            "differential derivation applies to python_row/frame_function stages"
        )
    if stage.output_schema is None:
        raise ValueError(
            f"stage {stage.id} has no output schema — a candidate's output cannot "
            "be compared without one"
        )
    if not stage.tests:
        raise ValueError(
            f"stage {stage.id} carries no frozen tests — there is no suite for "
            "candidates to pass, so nothing can be shown to underdetermine it"
        )
    if n < 2:
        raise ValueError(f"n must be >= 2 to differentiate, got {n}")


def _partition_by_frozen_tests(
    stage: Stage, candidates: list[DerivedCandidate]
) -> tuple[list[DerivedCandidate], list[str]]:
    """Split candidates into those that pass EVERY frozen test and those that do
    not; the second element is one human-readable note per rejected candidate."""
    passing: list[DerivedCandidate] = []
    notes: list[str] = []
    for candidate in candidates:
        results = run_stage_tests(_candidate_stage(stage, candidate))
        failures = [result for result in results if result.status != "passed"]
        if failures:
            statuses = ", ".join(f"{r.name}:{r.status}" for r in failures)
            notes.append(f"candidate {candidate.index} failed the frozen suite ({statuses})")
        else:
            passing.append(candidate)
    return passing, notes


def _find_divergence(
    stage: Stage, passing: list[DerivedCandidate], probe_inputs: list[InputBundle]
) -> AmbiguityFinding | None:
    """Run every surviving candidate on each probe and return the first probe on
    which any two candidates disagree — the representative divergence. Fewer than
    two survivors, or no probe that separates them, yields None (no ambiguity)."""
    if len(passing) < 2:
        return None
    assert stage.output_schema is not None  # _require_differentiable guarantees it
    columns = _output_columns(stage.output_schema)
    for probe in probe_inputs:
        outcomes = [run_transform(_candidate_stage(stage, c), probe) for c in passing]
        if _outcomes_diverge(columns, outcomes):
            return AmbiguityFinding(
                stage_id=stage.id,
                stage_name=stage.name,
                summary=(
                    f"{len(passing)} candidates that all pass the frozen suite for "
                    f"stage `{stage.id}` produce different behavior on an input the "
                    "frozen tests do not cover — the tests underdetermine this case"
                ),
                passing_candidate_count=len(passing),
                probe=DivergingProbe(
                    inputs=probe,
                    outputs=[
                        CandidateOutput(
                            candidate_index=candidate.index,
                            rows=outcome.rows,
                            error=outcome.error,
                        )
                        for candidate, outcome in zip(passing, outcomes)
                    ],
                ),
            )
    return None


def _outcomes_diverge(columns: list[str], outcomes: list[TransformOutcome]) -> bool:
    """Do any two candidate outcomes on the same probe disagree? Two rows-outputs
    disagree when they are not equal as a multiset; a rows-output and an error
    disagree (one defines the case, the other does not). Two errors are treated
    as agreement — the candidates concur the input is out of their domain, and
    an all-error probe is not ambiguity (mirrors the all-fail rule)."""
    for index_a in range(len(outcomes)):
        for index_b in range(index_a + 1, len(outcomes)):
            if _pair_diverges(columns, outcomes[index_a], outcomes[index_b]):
                return True
    return False


def _pair_diverges(columns: list[str], a: TransformOutcome, b: TransformOutcome) -> bool:
    if a.rows is not None and b.rows is not None:
        return not outputs_agree(columns, a.rows, b.rows)
    # One raised and the other produced rows: their behavior on this input differs.
    return (a.rows is None) != (b.rows is None)


def _candidate_stage(stage: Stage, candidate: DerivedCandidate) -> Stage:
    """`stage` with its function block replaced by `candidate`'s code, ready to
    run through the frozen tests or a probe. The schemas, inputs, and frozen
    tests are the stage's own — only the implementation varies across candidates."""
    return stage.model_copy(
        update={
            "function": PythonFunction(
                kind=FunctionKind.inline,
                code=candidate.code,
                function=candidate.function,
            )
        }
    )


def _output_columns(output_schema: TableSchema) -> list[str]:
    return [column.name for column in output_schema.columns]


def _agent_deriver(document: str, stage: Stage, *, model: str) -> DeriveCandidate:
    """The default derivation step: a fresh code-deriver agent per index, run
    headlessly. Each call builds its own Agent and awaits its own run(), so no
    derivation observes another's output — the independence the differential
    depends on."""
    async def derive(index: int) -> DerivedCandidate:
        agent: Agent[DerivedTransformCode] = Agent(
            system_prompt=_CODE_DERIVER_SYSTEM_PROMPT,
            target_schema=DerivedTransformCode,
            task=_render_code_task(document, stage),
            model=model,
        )
        answer = await agent.run()
        return DerivedCandidate(index=index, code=answer.code, function=answer.function)

    return derive


def _render_code_task(document: str, stage: Stage) -> str:
    """The code deriver's task: methodology + the stage's identity, schemas, and
    frozen tests (the acceptance criteria a candidate must satisfy). Every
    candidate sees the SAME material and never another candidate's code — where
    they fill a gap the material leaves open is exactly what the differential
    surfaces."""
    inputs = "\n\n".join(
        f"Input `{ref.id}` schema:\n{ref.table_schema.to_prompt()}"
        if ref.table_schema is not None
        else f"Input `{ref.id}` (no schema declared)"
        for ref in stage.inputs
    )
    assert stage.output_schema is not None
    tests = "\n".join(
        f"- {test.name}: inputs={test.inputs} -> expected={test.expected}"
        for test in (stage.tests or [])
    )
    return (
        f"----- METHODOLOGY DOCUMENT -----\n{document}\n"
        f"----- END DOCUMENT -----\n\n"
        f"Implement the transform for stage `{stage.id}` ({stage.type}): {stage.name}\n\n"
        f"{inputs}\n\n"
        f"Output schema:\n{stage.output_schema.to_prompt()}\n\n"
        f"Frozen tests the implementation MUST pass:\n{tests}"
    )


_CODE_DERIVER_SYSTEM_PROMPT = """\
You implement ONE stage of a data workflow as an inline python function. You are
given the methodology document, the stage's input/output schemas, and a frozen
set of tests (input rows -> exact expected output rows) your function must pass.

Write the function the stage type requires:
- python_row_function -> `def transform(row: dict) -> dict` (one row in, one row
  out; you never see the whole frame);
- python_frame_function -> `def transform(df, ...) -> DataFrame` (the input pandas
  DataFrame(s), positional in declared input order).

Your function MUST pass every frozen test. Beyond the tests, derive behavior from
the methodology — do not invent rules the document does not support, and do not
overfit to the exact test rows. Where the methodology and tests leave a case
genuinely underspecified, choose the reading the document best supports and
implement it directly; do not add configuration or ask.

Submit the finished code with the submit_answer tool. Do not restate it after."""
