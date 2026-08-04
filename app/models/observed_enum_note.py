"""When a column's `enum` may be declared, rendered into every prompt that authors a
schema, held ONCE so the authoring surfaces cannot drift apart on it. Lives beside
authoring_lifecycle_note.py for the same import-linter reason: app.models is the only
layer both app.agents (through app.tools) and app.mcp may read prose from."""
from __future__ import annotations

# Names no tool: the two authoring surfaces register different sets, so each states
# its own recipe after embedding this.
OBSERVED_ENUM_GUIDANCE = """\
Declaring an `enum` from observed data: author a categorical-looking column as a
bare type first, run the pipeline, then LOOK at the values that stage's output
really held before tightening the schema. Prose is where a wrong vocabulary comes
from — the document's three example statuses are not the file's three statuses.
Observing IS research, so it belongs before the signed-off plan.

The distinct COUNT is evidence, never the criterion. Two questions decide:
1. Is the value's GENERATION constrained to a discrete set — a dropdown on the
   source form, a published code list, an enum upstream? A column can hold
   thousands of values and still be closed (commodity codes), or three and still
   be open, because nothing stops a fourth. That is a claim about the world:
   research settles it, and the observed values confirm or refute it.
2. Do WE consume it as a discrete set — a later stage switching per value, or
   joining it into reference data? Then the enum is MANDATORY whatever was
   observed: an unlisted value otherwise takes an else-branch or joins to
   nothing, SILENTLY, and the declaration is what makes that loud. That is a
   design commitment, so it goes in the PLAN — the human signs off on the enum,
   not just the stage list.

A vocabulary read off a sliced run, off a frame below a filter or an aggregate,
or off a truncated value list is a SAMPLE, not the set. Say which one you have.

Two columns observation cannot settle for you: an `llm_transform` column whose
enum you already declared compiles into that stage's own reply model, so the run
returns a subset of your declaration and corroborates nothing; and a
`human_review_queue` decision column auto-approves every row in a test run, so
its values are an artifact of the test.

An enum never replaces guard code: a rule a declaration cannot state — a
cross-column consistency rule, normalization before comparison — still belongs
in the stage's authored code."""
