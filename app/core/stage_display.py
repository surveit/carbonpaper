"""How a stage type is shown to a reader: its glyph and its colour class. Keyed by
the type's string value, so this stays below the domain models."""
from __future__ import annotations

# Read by the web layer (diagrams, stage panels) and by the exported review packet,
# which vendors the app's stylesheet so a packet looks like the app it came from.
# One definition, so the two cannot drift.
#
# Every StageType must appear in both maps: an unmapped type falls back to `custom`,
# the red badge palette that elsewhere means error.
# tests/arch/test_stage_type_presentation.py fails when one is missing.

TYPE_CLASS = {
    "input_data": "input",
    "llm_transform": "llm",
    "python_row_function": "python",
    "python_frame_function": "python",
    "starlark_row_function": "python",
    "enrich": "join",
    "expand": "join",
    "aggregate": "aggregate",
    "human_review_queue": "human",
    "publish": "publish",
    # Row-set operations: union stacks frames, filter_rows drops subject rows.
    "union": "rowset",
    "filter_rows": "rowset",
}

TYPE_GLYPH = {
    "input_data": "⬆️",
    "llm_transform": "✨",
    "python_row_function": "🔂",
    "python_frame_function": "🧨",
    "starlark_row_function": "🛡️",
    "enrich": "🔗",
    "expand": "🌿",
    "aggregate": "📊",
    "human_review_queue": "👤",
    "publish": "📤",
    "union": "➕",
    "filter_rows": "🔽",
}
