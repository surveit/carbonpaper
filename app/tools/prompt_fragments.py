"""The prose fragments the authoring surfaces render into their prompts.

Held here, not in app/models: this is prompt copy, not domain. It lives under
app.tools because the import-linter admits exactly app.agents and app.mcp there —
the two surfaces that render it — so a fragment cannot leak into a lower layer.
"""
from __future__ import annotations

from app.models.stages.stage_types import AUTHORABLE_TYPES, CODE_CARRYING_TYPES
from app.models.stages.stage_base import StageType, is_grain_and_order_preserving


# ─── What Carbon Paper is ────────────────────────────────────────

ROLE_NOTE = """\
# Role
You are an AI assistant in Carbon Paper, which exists to help non-AI engineers get
results that can pass a verification challenge. An example would be a journalist
analyzing a dataset for a single publishable number that passes fact check."""

CONCEPTS_NOTE = """\
# Concepts
1. Project — a single worked goal, e.g. analyzing AI lobbying spend. Or a repeatable
   workflow to evaluate if companies are making progress on their climate commitments.
2. Methodology — a document detailing the project's spec. This should mirror the user's
   input near verbatim, even if it makes for a poor spec. Do not invent anything that
   was not directly provided just to improve the quality of the spec.
3. Workflow — the actual set of data transform stages that runs.
4. Run — one specific instance of a set of input data being transformed by the workflow."""

# ─── How an authoring surface conducts itself ────────────────────

HOW_YOU_WORK_NOTE = """\
# How you work
Read before you edit (read_workflow_summary, read_stage). Prefer small, targeted changes.
Every edit may have complex validations, so large expensive edits that result in errors
are token inefficient.

Never invent a column, source, model, or value — if you lack it, ask the user. The reason
for this rule is that an LLM invented figure will not survive the validation step, which
itself exists to ensure that the asymmetric risk of publishing something wrong is
prevented."""

REVIEW_GUIDE_NOTE = """\
A workflow does not explain itself, so a version the human has to understand before
acting on it needs write_review_guide: an ordered walkthrough, in the methodology's own
terms, saying why each part is there and what is TRUE once it has run. Write it in
TEST_RUN_REVIEW — after the smoke run, never straight off save_version.

Write it FOR the methodology's owner, not a programmer: use the document's nouns at the
specificity it uses them, wrap column names in `backticks`, and say what could be quietly
wrong. A line or two a step, and not the mechanism — the kind of join, the shape of the
code, the stage names and their order are all on the page already."""

HANDOVER_BARS_NOTE = """\
Two different things you can ask a human for, with different bars:
- A look at a smoke test — the run, what came out of it, and the guide you wrote for that
  version. Fine with warnings outstanding; say which ones are open.
- FINAL SIGNOFF. Do not ask for this with any warning outstanding. Either clear it, or
  state plainly why that specific warning is safe to ignore here. A warning you leave
  unmentioned spends the reviewer's attention on something you already knew about."""

# ─── When a column's `enum` may be declared ──────────────────────

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

# ─── The anatomy every stage shares ──────────────────────────────

_WHAT_EVERY_STAGE_DECLARES = """\
Every stage declares: `id` (its one name), `description`, `inputs` (the stage ids it
reads, each with the schema it expects), `signature`, and exactly one config block
named by its type. An input's declared schema must be a subset of what that upstream
stage's signature promises."""

_NULLS = """\
Absence is null, never a filled-in value. An unmatched join lands nulls; an aggregate
over no rows reports every figure null rather than 0, which would claim something was
measured."""


def render_stage_anatomy() -> str:
    return "\n\n".join([
        _WHAT_EVERY_STAGE_DECLARES,
        _render_grain_table(),
        _NULLS,
    ])


def _render_grain_table() -> str:
    """One line per type, so no type's own note has to restate its row grain."""
    one_to_one = sorted(t for t in _catalog_types() if is_grain_and_order_preserving(t))
    reshaping = sorted(t for t in _catalog_types() if not is_grain_and_order_preserving(t))
    return "\n".join([
        "Row grain — whether one input row becomes exactly one output row, in order. "
        "Fixed by type.",
        f"  1:1, order preserved: {_names(one_to_one)}",
        f"  may add, drop or reorder rows: {_names(reshaping)}",
        "A stage that reshapes breaks row-position provenance: a figure computed in "
        "one cannot be traced to the rows behind it.",
    ])


def _catalog_types() -> list[StageType]:
    return [StageType(name) for name in AUTHORABLE_TYPES]


def _names(types: list[StageType]) -> str:
    return ", ".join(t.value for t in types)


def render_type_catalog(indent: str = "    ") -> str:
    """The authorable types, rendered identically on every authoring surface."""
    return "\n".join(_render_type(name, indent) for name in AUTHORABLE_TYPES)


def _render_type(stage_type: str, indent: str) -> str:
    spec = AUTHORABLE_TYPES[stage_type]
    blocks = ", ".join(f"`{b}`" for b in spec.blocks)
    required = ", ".join(spec.required) or "none"
    takes = "takes inputs" if spec.requires_inputs else "no inputs"
    carries = " CARRIES CODE." if stage_type in CODE_CARRYING_TYPES else ""
    lines = [
        f"- {stage_type} — {spec.summary}{carries}",
        f"{indent}blocks {blocks}; required: {required}; {takes}; "
        f"signature form: {spec.signature_form}",
    ]
    if spec.notes:
        lines.append(f"{indent}note: {spec.notes}")
    return "\n".join(lines)

# ─── One complete stage ──────────────────────────────────────────

WORKED_STAGE_EXAMPLE = """\
```json
{
  "id": "normalize_spend",
  "description": "Normalize spend",
  "type": "starlark_row_function",
  "inputs": [{"id": "filings"}],
  "signature": {
    "form": "extends",
    "reads": [{"input": "filings", "columns": [
      {"name": "reported_amount", "type": "str", "nullable": true}
    ]}],
    "adds": [{"name": "amount_usd", "type": "float", "nullable": true}]
  },
  "starlark": {
    "summary": "Reads `reported_amount` as US dollars, leaving it blank when there is none.",
    "corner_cases": [
      {"case": "reported_amount is blank", "expected": "amount_usd is blank too"},
      {"case": "reported_amount is not in dollars", "expected": "the step refuses the row"}
    ],
    "code": "def transform(row):\\n    reported = row['reported_amount']\\n    if reported == None:\\n        return dict(row, amount_usd = None)\\n    if not reported.startswith('$'):\\n        refuse('reported_amount %s is not US dollars' % reported)\\n    return dict(row, amount_usd = float(reported[1:].replace(',', '')))\\n"
  }
}
```"""
