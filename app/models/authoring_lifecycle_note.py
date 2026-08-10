"""The five compiler phases (CompilerPhase) and the gated authoring lifecycle prose
rendered into every prompt that can invent schema or artifacts, held ONCE so the
generating surfaces cannot drift apart on them. Lives in app.models (import this
submodule, not the aggregator — at its fan-out ceiling) because the import-linter walls
admit only app.services into app.compiler and only app.agents/app.mcp into app.tools."""
from __future__ import annotations

from enum import StrEnum


class CompilerPhase(StrEnum):
    RESEARCH = "research"
    PLANNING = "planning"
    BUILD = "build"
    TEST_RUN = "test_run"
    TEST_RUN_REVIEW = "test_run_review"


# The slice the data-model prompt embeds on its own; the full lifecycle below
# embeds it verbatim, and tests/test_authoring_lifecycle_prompt.py pins both.
INTERMEDIATE_CONCEPTS_NOTE = """\
Every intermediate concept the design adds beyond what the user asked for —
a join key, a generated ID, a bookkeeping column — goes in the plan with
the reason it is needed; the user reads that plan against the mental model
of THEIR OWN data."""

# The same five steps in one line. The home zero state renders it to set a first-time
# reader's expectations, so what the product promises and what the prompts enforce are
# one string.
LIFECYCLE_ONE_LINE = (
    "research, a plan you sign off, the build, a smoke run before the full one"
)

# The full lifecycle, for the surfaces that author the workflow end to end (the
# editing agent's prompt and the MCP instructions). Each numbered step is
# headed by its CompilerPhase name, which test_authoring_lifecycle_prompt.py pins.
AUTHORING_LIFECYCLE_GUIDANCE = f"""\
A project runs in five phases: RESEARCH, PLANNING, BUILD, TEST_RUN,
TEST_RUN_REVIEW. Authoring is gated: {LIFECYCLE_ONE_LINE}.
1. RESEARCH FIRST. Read the prose and the real data. Building and running
   a prototype pipeline over limited rows IS research — to learn
   how the data shapes out through the stages. The gates govern committal, not exploration:
   a prototype is scaffolding, never the deliverable.
2. PLANNING — PLAN, AND ASK QUESTIONS. Ask what research left open rather than guess.
   The sign-off gate: the plan names the major stages and clears the rule below.
{INTERMEDIATE_CONCEPTS_NOTE}
3. BUILD TO THE SIGNED-OFF PLAN. A mid-build deviation goes back to the user,
   never silently into the output; agreed additions and their reasons go in the
   stage's `compiler_notes`. A stage's example tests pass here, not after the run.
4. TEST_RUN — SMOKE BEFORE FULL. With expensive compute stages, run under row limits
   first, and read that output yourself: this is the phase that sends you back to BUILD.
5. TEST_RUN_REVIEW — WRITE THE GUIDE LAST. A guide written before the run describes
   stages the run has since changed. Then hand over together: the smoke output, its guide,
   and the warnings you did not clear — that sign-off spends the full-run budget."""
