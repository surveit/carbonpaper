# Methodology DAG Schema v2 — Executable Node Types

This is the contract a compiled stage file must satisfy. Every stage is *executable* in principle: it declares typed inputs, a typed output, and an executable handle (connector / prompt / function / join / aggregation / queue / publish). The compiler does not produce prose blobs dressed as structured data.

## The eight stage types

| Type | What it does | Executable handle |
|---|---|---|
| `input_data` | Declares a source dataset with a typed schema. | `connector:` block (file/computed_static) |
| `llm_transform` | Row-by-row LLM call producing structured output. | `llm:` block (model + prompt template + rubric) |
| `python_row_function` | Python mapped **per input row** by the runtime — one row in, one row out. Single input. | `function:` block (inline code or module:fn ref) |
| `python_frame_function` | Python over the whole upstream frame(s) — may reshape (group-by, pivot, dedup, multi-input merge). | `function:` block |
| `join` | Combine two or more upstream dataframes on keys. | `join:` block (keys + type) |
| `aggregate` | Structured group-by aggregation. | `aggregate:` block (group_by + ops) |
| `human_review_queue` | Pulls flagged rows from upstream, emits reviewed rows. | `queue:` block |
| `publish` | Render final artifact (table, json, html, evidence cards). | `publish:` block |

**Strongly prefer `python_row_function`.** It receives one row (a dict) and returns one row (a dict); the runtime maps it, so its 1:1 grain is *guaranteed* (not merely claimed) and evals can score straight through it. Reach for `python_frame_function` only when the logic genuinely needs the whole frame — grouping, ranking, deduping, or merging more than one input — and say why in a compiler_note. Anything that doesn't fit a structured type is a `python_frame_function` (or a `python_row_function` if it's a pure per-row map) with a compiler_note explaining why.

## Universal stage shape

On disk each compiled stage is the JSON dump of the `Stage` model; the example below uses YAML notation for readability.

```yaml
id: snake_case_id
name: Human readable name
type: <one of the eight stage types above>

source:
  doc: examples/<project>/stages/NN_<stage>.md
  section: "§N.M"
  lines: [start, end]

inputs:
  # required for every stage except input_data
  - id: upstream_stage_id
    schema:
      columns:
        - {name: col_a, type: str, nullable: false, description: "..."}
        - {name: col_b, type: float, nullable: true, range: [0.0, 1.0]}
      primary_key: [col_a]

output_schema:
  columns:
    - {name: col_x, type: str, nullable: false}
    - {name: col_y, type: int, nullable: false, range: [-2, 2]}
  estimated_rows: 30000
  primary_key: [col_x]

# ONE of the executable-handle blocks below, matching the stage type:

connector:        # input_data only
  kind: file
  params:
    path: examples/<project>/data/source.csv
    format: csv
  refresh: yearly
  notes: |
    ...

llm:              # llm_transform only
  model: claude-sonnet-4-6
  temperature: 0.0
  max_retries: 3
  response_format: json
  prompt_template: |
    Score this evidence... {evidence_text}
    ...
  rubric:         # optional structured rubric the prompt references
    "+2": ...
    "-2": ...

function:         # python_row_function / python_frame_function
  kind: inline    # or module
  code: |
    import pandas as pd
    def transform(scored: pd.DataFrame, tagged: pd.DataFrame) -> pd.DataFrame:
        ...
        return result
  requirements: [pandas]

join:             # join only
  type: inner     # or left, right, outer
  keys:           # named `keys` rather than `on` (a name chosen when stages were authored in YAML, where bare `on` parses as a boolean)
    - {left: evidence_id, right: evidence_id}

aggregate:        # aggregate only
  group_by: [entity_id, source_class, policy_query]
  aggregations:
    - {output_column: cell_score, formula: mean, value_column: score}
    - {output_column: evidence_count, formula: count}

queue:            # human_review_queue only
  reviewer_instructions: |
    Verify the score by reading the quote and checking against the rubric...
  conflict_resolution: third_reviewer

publish:          # publish only
  format: html_report   # or json, csv, evidence_cards
  template: examples/lobbymap/templates/org_profile.html
  destination: build/

eval:             # mostly llm_transform; required when claiming a quality measurement
  reference: examples/<project>/eval_data/<file>.csv
  reference_schema:
    columns:
      - {name: id, type: str}
      - {name: human_label, type: int}
  join_on: [id]
  metrics:
    - exact_agreement_rate
    - confusion_matrix_5x5

review:           # any stage may have a review block on its outputs
  when: "abs(score) >= 2"
  routing: 1-of-2-disagreement-escalates
  rationale: |
    ...

compiler_notes:
  - "Any honest flag about prose ambiguity, stretched type choice, etc."
```

## Type vocabulary for column types

```
str              — text
int              — integer
float            — floating point
bool             — boolean
datetime         — ISO datetime
date             — ISO date (no time)
list[str]        — list of strings
list[<type>]     — generic list
dict             — arbitrary key-value
json             — opaque JSON blob (use sparingly)
```

`nullable: true` means the column may be missing/null. `range: [low, high]` for numerics; `range: [enum1, enum2, ...]` for categorical strings.

## Connector kinds (for `input_data`)

```
file            — local file. params: {path, format}
computed_static — curated list with no automated fetch (e.g., the project's own benchmark library)
```

## Aggregation formulas

```
sum, mean, count, min, max, first, list      — standard (all but count require value_column)
```

Weighted aggregation is not part of this contract — do it inside a
`python_frame_function` instead. Anything else non-trivial also belongs in a
`python_frame_function`, not in an `aggregate`.

## Filling in details from prose

Prose translation rules:

- **Numbers in prose become `range:` constraints in schemas.** "Importance score 0–10" → `{type: int, range: [0, 10]}`.
- **Lists of categories in prose become `range:` enums.** "Source classes: org_websites, corporate_media, CDP, ..." → `{type: str, range: [org_websites, corporate_media, CDP, ...]}`.
- **Formulas in prose become inline pandas code.** Don't produce `formula: "0.5 * direct + 0.5 * indirect"` as a string — write the function. If the prose is underspecified, write a default function and add a compiler_note.
- **"Methodology says X" sentences are not stage parameters.** They go into compiler_notes if they describe an ambiguity, or into the `description:` field of the relevant column/parameter if they're documentation.
- **Rubrics with verbatim score descriptions stay verbatim.** Pull Table 6 (or equivalent) into the `llm.rubric:` field literally.

## Eval and review — when to add

Add `eval:` when an LLM stage produces a label or score that ground truth could exist for. Reference paths can point to files that don't exist yet — the user will populate them from prior work. The `reference_schema` field is what makes eval real: it tells the runtime what columns to expect in the ground-truth file, so it can complain at upload time rather than at run time.

Add `review:` whenever the output has asymmetric error cost, the prose explicitly demands human verification, or the LLM stage is high-stakes. The `when:` field is a predicate over the stage's output columns. Routing options: `1-of-1`, `2-of-2`, `1-of-2-disagreement-escalates`, `random_sample_10pct`, etc.

## Compiler notes — be honest

This is the channel where the compiler tells the human what's underspecified, where a type choice was a stretch, where the prose has gaps. Empty list is fine for a clean compilation. Quality of compiler_notes is a first-class output, not a footnote.

Common categories of compiler notes:
- "Formula reconstructed; prose at line N doesn't give closed form."
- "Recency-decay shape chosen by compiler (linear over 5 years); prose ambiguous."
- "Stage type stretched: this is technically X but Y aspect doesn't fit."
- "Default model chosen; prose doesn't specify."
- "Schema field N inferred from upstream stage; prose doesn't enumerate."

If a stage has more than ~5 compiler notes, the methodology prose is probably underspecified at that point and the user should be prompted to refine it.
