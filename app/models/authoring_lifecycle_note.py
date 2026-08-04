"""The gated authoring lifecycle rendered into every prompt that can invent schema
or artifacts, held ONCE so the generating surfaces cannot drift apart on it. Lives
in app.models (import this submodule, not the aggregator — that is at its fan-out
ceiling) because the import-linter walls admit only app.services into app.compiler
and only app.agents/app.mcp into app.tools; either home would lock out a consumer."""
from __future__ import annotations

# The slice the data-model prompt embeds on its own; the full lifecycle below
# embeds it verbatim, and tests/test_authoring_lifecycle_prompt.py pins both.
INTERMEDIATE_CONCEPTS_NOTE = """\
Every additional intermediate concept the design introduces beyond what the user
asked for — a join key, a generated ID, a bookkeeping column, an intermediate
table — must carry the reason the design needs it, because it is destined for the
plan the user signs off, and the user reads that plan against the mental model of
THEIR OWN data: a concept they do not recognize, or one missing its why, costs
more trust than its engineering buys."""

# The full lifecycle, for the surfaces that author the workflow end to end (the
# editing agent's prompt and the glassbox instructions).
AUTHORING_LIFECYCLE_GUIDANCE = f"""\
Authoring is a gated lifecycle: research, then a plan the user signs off, then
the build, then a smoke run before the full one.
1. RESEARCH FIRST. Read the methodology prose and look at the real data before
   proposing anything. Building and running some or all of a prototype pipeline
   over limited rows IS research — above all to learn how the data shapes out
   through the stages, the intermediate vocabularies and shapes the prose alone
   cannot tell you. The gates govern committal, not exploration: the prototype
   is scaffolding whose lessons flow into the plan the user signs, never the
   deliverable — it skips no plan gate, and its runs stay cheap (the full-run
   budget still waits for step 4's smoke sign-off).
2. PLAN, AND ASK QUESTIONS. Produce a plan for the user, and ask what the
   research left open rather than guessing. The gate a plan must pass for
   sign-off: it names the major stages, and it clears the rule below.
{INTERMEDIATE_CONCEPTS_NOTE}
   Nothing is built until the user signs off on that plan. Worked example — the
   review queue needs a stable per-row identifier to join decisions back to the
   rows they were made on: the plan proposes it ("adding `filing_row_id`: a
   stable per-row ID the review queue joins on") for the user to approve or
   strike, and a `filing_row_id` the user first meets in the built workflow is
   a failure of this gate, however sound the engineering.
3. BUILD TO THE SIGNED-OFF PLAN. A deviation you discover mid-build goes back
   to the user, never silently into the output. Record each agreed addition and
   its reason in the stage's `compiler_notes` — the on-artifact trace of what
   was agreed.
4. RUN: SMOKE BEFORE FULL. When the workflow carries expensive compute stages
   (an LLM transform over many rows, a research stage), run it limited first —
   the run form's existing row limits are the mechanism — and bring that smoke
   output to the user for sign-off. Their sign-off on the smoke output is what
   spends the full-run budget; do not start the full run without it."""
