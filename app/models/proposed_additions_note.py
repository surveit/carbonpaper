"""The plan-agreement rule rendered into every prompt that can invent schema or
artifacts, held ONCE so the generating surfaces cannot drift apart on it. Lives in
app.models (imported from this submodule, not the aggregator) because the
import-linter contracts admit only app.services into app.compiler and only
app.agents/app.mcp into app.tools — either home would lock out one consumer."""
from __future__ import annotations

# Embedded verbatim by the data-model prompt, the editing agent's prompt and the
# glassbox instructions; tests/test_proposed_additions_prompt.py asserts the embedding.
PROPOSED_ADDITIONS_GUIDANCE = """\
Anything you add that nobody asked for must be AGREED, never slipped in. Every
column, field, ID, or artifact in your output must be traceable to the methodology
prose or to something the user explicitly asked for. When the design genuinely
needs one the user did not ask for — a join key, a generated ID, a bookkeeping
column — do not silently add it: surface it as a PROPOSED ADDITION in the plan or
summary the human reviews and, where your output carries them, in the stage's
`compiler_notes`, stating what was added and why the design needs it. The reader
of your output is the methodology's author checking it against the mental model of
THEIR OWN data: their confidence comes from the artifact matching what they
expected, so an addition they do not recognize costs more trust than its
engineering buys. Worked example — you need a stable per-row identifier so the
review queue's decisions can be joined back to the rows they were made on. Wrong:
silently emit a `filing_row_id` column and move on. Right: propose it in the plan
— "I need a stable per-row ID for the review queue join; may I add
`filing_row_id`?" — or, when no one can answer mid-turn, add it flagged
prominently as a proposed addition in the plan summary and in `compiler_notes`,
so the author can strike it rather than discover it."""
