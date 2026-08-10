"""When a column's `enum` may be declared, rendered into every prompt that authors a
schema, held ONCE so the authoring surfaces cannot drift apart on it. Lives beside
authoring_lifecycle_note.py for the same import-linter reason: app.models is the only
layer both app.agents (through app.tools) and app.mcp may read prose from."""
from __future__ import annotations

# Names no tool: the two authoring surfaces register different sets, so each states
# its own recipe after embedding this.
ENUM_FROM_DATA_GUIDANCE = """\
Declaring an `enum`: author a categorical-looking column as a bare type first, run
the pipeline, then LOOK at what that stage's output really held before tightening
the schema. The document's three example statuses are not the file's three.

The distinct COUNT is evidence, never the criterion. Two questions decide:
1. Is the value's GENERATION constrained to a discrete set — a dropdown on the
   source form, a published code list, an enum upstream? A column can hold
   thousands of values and still be closed (commodity codes), or three and still
   be open. That is a claim about the world: research settles it, and the values
   you read confirm or refute it.
2. Do WE consume it as a discrete set — a later stage switching per value, or
   joining it into reference data? Then the enum is MANDATORY whatever was
   you read: an unlisted value otherwise takes an else-branch or joins to nothing,
   SILENTLY. That is a design commitment, so it goes in the PLAN.

Values read off a sliced run, off a frame below a filter or an aggregate, or off a
cut value list are a SAMPLE, not the set. Say which one you have. Say which one you have."""
