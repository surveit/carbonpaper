"""The gated authoring lifecycle rendered into every prompt that can invent schema
or artifacts, held ONCE so the generating surfaces cannot drift apart on it. Lives
in app.models (import this submodule, not the aggregator — that is at its fan-out
ceiling) because the import-linter walls admit only app.services into app.compiler
and only app.agents/app.mcp into app.tools; either home would lock out a consumer."""
from __future__ import annotations

# The slice the data-model prompt embeds on its own; the full lifecycle below
# embeds it verbatim, and tests/test_authoring_lifecycle_prompt.py pins both.
INTERMEDIATE_CONCEPTS_NOTE = """\
Every intermediate concept the design adds beyond what the user asked for —
a join key, a generated ID, a bookkeeping column — goes in the plan with
the reason it is needed; the user reads that plan against the mental model
of THEIR OWN data."""

# The full lifecycle, for the surfaces that author the workflow end to end (the
# editing agent's prompt and the glassbox instructions).
AUTHORING_LIFECYCLE_GUIDANCE = f"""\
Authoring is gated: research, a signed-off plan, the build, a smoke run before
the full one.
1. RESEARCH FIRST. Read the prose and the real data. Building and running
   a prototype pipeline over limited rows IS research — to learn
   how the data shapes out through the stages. The gates govern committal, not exploration:
   a prototype is scaffolding, never the deliverable.
2. PLAN, AND ASK QUESTIONS. Ask what research left open rather than guess. The
   sign-off gate: the plan names the major stages and clears the rule below.
{INTERMEDIATE_CONCEPTS_NOTE}
   Worked example: the plan proposes "adding `filing_row_id`: a stable per-row
   ID the review queue joins on" to approve or strike; first met in the built
   workflow, it is a failure of this gate.
3. BUILD TO THE SIGNED-OFF PLAN. A mid-build deviation goes back to the user,
   never silently into the output; agreed additions and their reasons go in the
   stage's `compiler_notes`.
4. RUN: SMOKE BEFORE FULL. With expensive compute stages, run under row limits
   and bring the smoke output for sign-off — that sign-off spends
   the full-run budget."""
