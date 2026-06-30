"""
prompt.py — the COMPILER's system prompt + prompt builder.

The compiler's whole job is one LLM call: hand the model an UNSTRUCTURED account
of a research process (a captured agent/tool transcript, a set of notes, or plain
prose) and ask it to emit a structured methodology DAG that validates against
`app/dag_schema.py`. This module owns *how we ask* — kept separate from
`app/compiler.py` (which owns the mechanism: call, parse, validate, persist).

`_node_type_contract()` renders the contract straight from `dag_schema.NODE_TYPES`
so the prompt can never drift from the real schema.
"""

from __future__ import annotations

import json

from app import dag_schema


# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a METHODOLOGY COMPILER. You read an UNSTRUCTURED account of one "
    "research process — it may be a captured agent/tool transcript, a set of "
    "notes, or plain prose describing how an investigation was carried out — and "
    "you DISTILL it into a reusable, structured methodology DAG of typed stages "
    "targeting app/dag_schema.\n\n"
    "You have NO tools and NO web access: work only from the text the user gives "
    "you. Put the LLM (llm_transform) only at the FEW genuine judgment points; "
    "everything mechanical (building queries, downloading, parsing, joining, "
    "aggregating, rendering) must be a deterministic node type.\n\n"
    "Respond with raw JSON exactly matching the requested shape — no prose, no "
    "markdown, no code fences. Never fabricate data values, URLs, numbers, or "
    "sources; when the input is ambiguous, encode your best STRUCTURAL guess in "
    "the stage and record the uncertainty in compiler_notes. Structure is yours "
    "to infer; facts are not."
)


# ─────────────────────────────────────────────────────────────────────────────
# The dag_schema contract, rendered into the prompt (single source of truth)
# ─────────────────────────────────────────────────────────────────────────────

def _node_type_contract() -> str:
    """Render the 7 node types + their handle blocks straight from dag_schema, so
    the prompt can never drift from the real contract."""
    out: list[str] = ["The 7 node types (each carries its executable-handle block):\n"]
    for tname, spec in dag_schema.NODE_TYPES.items():
        handle = spec["handle"]
        req = ", ".join(spec["required"]) or "(none)"
        opt = ", ".join(spec.get("optional", [])) or "(none)"
        also = spec.get("also_requires", [])
        also_s = f"; also needs block(s): {', '.join(also)}" if also else ""
        out.append(
            f"- **{tname}** — {spec['summary']}\n"
            f"    handle block: `{handle}:` required fields=[{req}] optional=[{opt}]{also_s}\n"
            f"    min_inputs={spec['min_inputs']}, requires_inputs={spec['requires_inputs']}"
        )
    out.append("")
    out.append("Column types: " + ", ".join(sorted(dag_schema.SCALAR_COLUMN_TYPES))
               + ", or list[<type>].")
    out.append("Connector kinds (input_data.connector.kind): "
               + ", ".join(sorted(dag_schema.CONNECTOR_KINDS)) + ".")
    out.append("python_transform.function.kind ∈ {module, inline} "
               "(module → needs `module`; inline → needs `code`).")
    out.append("join.type ∈ " + ", ".join(sorted(dag_schema.JOIN_TYPES))
               + "; join needs `keys`. publish also needs a `function:` block.")
    return "\n".join(out)


# A single concrete, schema-valid example stage so the model copies the exact key
# layout (handle block, inputs-with-schema, output_schema, snake_case id).
_EXAMPLE_STAGE = {
    "id": "locate",
    "name": "Locate the authoritative most-recent doc (LLM judgment)",
    "type": "llm_transform",
    "source": {"doc": "methodology_raw.md", "section": "§3"},
    "inputs": [
        {
            "id": "build_queries",
            "schema": {
                "primary_key": ["facility_id"],
                "columns": [
                    {"name": "facility_id", "type": "str"},
                    {"name": "name", "type": "str"},
                    {"name": "queries_json", "type": "json"},
                ],
            },
        }
    ],
    "llm": {
        "model": "haiku",
        "temperature": 0.0,
        "response_format": "json",
        "tools": ["WebSearch"],
        "prompt_template": "Find the authoritative most-recent doc for {name}. Return JSON ...",
    },
    "output_schema": {
        "primary_key": ["facility_id", "url"],
        "columns": [
            {"name": "facility_id", "type": "str"},
            {"name": "url", "type": "str"},
            {"name": "doc_type", "type": "str"},
            {"name": "is_primary", "type": "bool"},
        ],
    },
    "compiler_notes": ["JUDGMENT point: which doc is authoritative is not a fixed URL."],
}


def build_compile_prompt(input_text: str, name: str) -> str:
    """Assemble the full distillation prompt: the contract + a worked example + the
    unstructured input handed in verbatim.

    `input_text` is the raw account (transcript jsonl text, notes, or prose) — we
    do NOT pre-parse it; the model recovers the pipeline. `name` is the subject /
    out-name the compiled methodology is for."""
    contract = _node_type_contract()
    example = json.dumps(_EXAMPLE_STAGE, indent=2)

    return f"""\
You are distilling ONE research process into a reusable methodology DAG that would
reproduce this CLASS of research deterministically — with the LLM sitting at only
the FEW genuine judgment points and everything else as deterministic mechanism.

Subject / out-name: "{name}"

# The output contract (target: app/dag_schema.py)
Emit a methodology as a list of STAGE dicts. Each stage validates against this
contract:

{contract}

Universal stage keys: id (snake_case), name, type, inputs (list of
{{id, schema:{{columns:[{{name,type}}], primary_key:[...]}}}}), output_schema (same
shape), source, compiler_notes (list of strings). The executable-handle block
(connector / llm / function / join / aggregate / queue / publish) is keyed by the
node type as shown above.

Here is ONE complete, valid example stage — copy this exact key layout:

{example}

# The research process to distill
This is an UNSTRUCTURED account — it may be an agent/tool transcript, working
notes, or prose. Read it and recover the PIPELINE it implies; do not quote it.

<<<INPUT
{input_text}
INPUT>>>

# How to distill (the core insight)
Recover the step sequence the account implies and classify each step into a node
type — most steps are deterministic mechanism, a FEW are genuine judgment:
- A known seed identity (what was true going in) → an **input_data** stage.
- Turning identity into search strings / query construction → **python_transform**.
- Deciding WHICH found source is authoritative / most-recent → **llm_transform**
  (a real judgment point).
- Downloading a URL, converting a document to text, grepping fixed anchor keys →
  each a **python_transform** (deterministic mechanism, NOT an LLM stage).
- Reading a document's text into a structured field set → **llm_transform** (EXTRACT).
- Reconciling conflicting figures across sources/years → **llm_transform** (ADJUDICATE)
  or a **human_review_queue** if it is low-volume / high-stakes.
- Merging per-source rows back to one row per subject → **join** or python_transform.
- Rendering the final output → **publish**.

Keep llm_transform stages to the few real judgment points; make the rest
deterministic. Wire `inputs` so the DAG is connected and acyclic: every input id
must be the id of an upstream stage. Keep every id snake_case.

# Output format — RAW JSON ONLY, no prose, no markdown fences:
{{
  "stages": [ <list of stage dicts as above> ],
  "methodology_raw_md": "<a markdown methodology write-up: numbered sections, one
      per stage, describing what it does and why — the human-readable spec>",
  "compiler_notes": [ "<global ambiguities, judgment calls, things a human should
      confirm — e.g. is ADJUDICATE an LLM or a review queue?>" ]
}}

Do not fabricate data values. If a step's behaviour is ambiguous, encode your best
structural guess in the stage and record the ambiguity in compiler_notes. Output
the JSON object now."""
