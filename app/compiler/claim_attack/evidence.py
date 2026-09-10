"""The evidence bundle as the text an attacker reads and copies its backing out of."""
from __future__ import annotations

from app.models.claim_review import (
    BranchEvidenceItem,
    EvidenceBundle,
    InputColumnEvidenceItem,
    OutputEvidenceItem,
    StageEvidenceItem,
)

_FIELD = " · "
_NOTHING = "none"


def render_evidence_bundle(bundle: EvidenceBundle) -> str:
    blocks = [
        _render_the_claim(bundle),
        _render_outputs(bundle.outputs),
        _render_stages(bundle.stages),
        _render_branches(bundle.branches),
        _render_input_columns(bundle.input_columns),
        _render_heading("TERMS") + "\n" + (bundle.terms or _NOTHING),
        _render_heading("METHODOLOGY") + "\n" + (bundle.methodology or _NOTHING),
    ]
    return "\n\n".join(blocks)


def _render_the_claim(bundle: EvidenceBundle) -> str:
    shape = bundle.shape
    return "\n".join([
        # The sentence stays a substring of the corpus: literal marks, not repr escapes.
        f"CLAIM: «{bundle.claim_text}»",
        f"context: {_render_context(bundle)}",
        f"cited: stage `{bundle.cited.stage_id}`, row {bundle.cited.row_ordinal}, "
        f"column `{bundle.cited.column}`",
        f"shape: {shape.label}{_FIELD}universe: {shape.universe}{_FIELD}"
        f"importance: {shape.importance}",
        f"qualifiers: {'; '.join(shape.qualifiers) or _NOTHING}",
        f"the run read everything it was pointed at: {_render_flag(bundle.run_read_everything)}",
    ])


def _render_outputs(outputs: list[OutputEvidenceItem]) -> str:
    lines = [
        _FIELD.join([output.slug, output.label, output.value, output.stage_id]
                    + (["CITED"] if output.cited else []))
        for output in outputs
    ]
    return _render_block("OUTPUTS", lines)


def _render_stages(stages: list[StageEvidenceItem]) -> str:
    lines = []
    for stage in stages:
        lines.append(f"Stage `{stage.stage_id}` ({stage.type}, feeds the cited stage: "
                     f"{_render_flag(stage.feeds_the_cited_stage)}): {stage.description}")
        lines.append(f"  reads: {', '.join(stage.input_ids) or _NOTHING}")
        if stage.code:
            lines.extend(["  ```", *_indent(stage.code), "  ```"])
    return _render_block("STAGES", lines)


def _render_branches(branches: list[BranchEvidenceItem]) -> str:
    lines = []
    for branch in branches:
        lines.append(_FIELD.join([
            branch.branch_id, branch.stage_id, f"{branch.reason}/{branch.role}",
            f"rows {branch.rows_count}", branch.label or _NOTHING]))
        if branch.source_code:
            lines.extend(["  source:", *_indent(branch.source_code)])
    return _render_block("BRANCHES", lines)


def _render_input_columns(columns: list[InputColumnEvidenceItem]) -> str:
    lines = [
        _FIELD.join([
            f"{column.stage_id}.{column.column}", column.kind, f"rows {column.row_count}",
            f"{column.filled_count} filled/{column.null_count} null/"
            f"{column.blank_count} blank",
            f"distinct {column.distinct_count}", f"top: {_render_top(column)}"])
        for column in columns
    ]
    return _render_block("INPUT COLUMNS", lines)


def _render_top(column: InputColumnEvidenceItem) -> str:
    return ", ".join(f"{seen.value} ({seen.count})" for seen in column.top) or _NOTHING


def _render_context(bundle: EvidenceBundle) -> str:
    return ", ".join(f"{name} = {value}"
                     for name, value in bundle.claim_context.items()) or _NOTHING


def _render_block(name: str, lines: list[str]) -> str:
    return "\n".join([_render_heading(name), *(lines or [_NOTHING])])


def _render_heading(name: str) -> str:
    return f"----- {name} -----"


def _render_flag(held: bool) -> str:
    return "true" if held else "false"


def _indent(code: str) -> list[str]:
    return [f"  {line}" for line in code.strip("\n").splitlines()]
