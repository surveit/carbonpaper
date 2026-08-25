"""View-model for show-your-work: fold a linear trace (app.runtime.trace) plus the
compiled stages into the payload the template renders.

The payload is a graph (`nodes` + `edges`) even though v1 traces a single chain,
so real fan-in slots in without reshaping this contract.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePath
from typing import Any

from app.models import WorkflowStage
from app.models.stages.code import PythonFrameFunctionStage, PythonRowFunctionStage
from app.models.stages.input_data import InputDataStage
from app.models.stages.join import EnrichStage, ExpandStage
from app.web.stage_prose import say_what_a_stage_did
from app.models.stages.llm_transform import LLMTransformStage
from app.models.stages.signature import list_read_column_names
from app.models.stages.starlark import StarlarkRowFunctionStage
from app.runtime.lineage import EdgeKind
from app.services.loader import resolve_function_code
from app.web.config import label_stage_type, render_row_number
from app.web.panel_links import (
    CONTRIBUTOR_ROWS_LINKED,
    CONTRIBUTORS_NAMED,
    PanelLinks,
)
from app.web.trace_row_diff import build_row_diff, row_diff_to_dict

# How many contributing rows ONE cohort's link addresses. A `group_by: []`
@dataclass(frozen=True)
class ContributorGroup:
    stage_id: str
    # None where the producer did not attribute its contribution to particular
    # output columns; otherwise the columns every row in this cohort fed.
    columns: list[str] | None
    total: int
    # How many of them `rows_link` opens — `total` where it opens all of them.
    linked: int
    # Parent entries shaped exactly like `branches`, so the page renders them
    # the same way. Empty unless the cohort is small enough to name row by row.
    named: list[dict[str, Any]]
    rows_link: str | None




def _transform_of(workflow_stage: WorkflowStage | None) -> dict[str, Any]:
    if workflow_stage is None:
        return {"kind": "unknown", "detail": None}
    stage = workflow_stage.stage
    if isinstance(stage, InputDataStage):
        named = ", ".join(stage.connector.params.paths)
        src = named or (stage.source.doc if stage.source else None)
        return {"kind": "source", "detail": src or "originates the rows"}
    if isinstance(stage, PythonFrameFunctionStage):
        return {"kind": "python_frame", "detail": resolve_function_code(stage)}
    if isinstance(stage, PythonRowFunctionStage):
        # Full source: the whole module file for a module ref, the inline code
        # for an inline ref — never a partial snippet or a bare reference.
        return {"kind": "python", "detail": resolve_function_code(stage)}
    if isinstance(stage, StarlarkRowFunctionStage):
        return {"kind": "starlark", "detail": resolve_function_code(stage)}
    if isinstance(stage, LLMTransformStage):
        return {"kind": "llm", "detail": {
            "instructions": stage.llm.prompt_instructions,
            "data_template": stage.llm.prompt_data_template,
        }}
    if isinstance(stage, (EnrichStage, ExpandStage)):
        pairs = stage.join.keys
        detail = ", ".join(f"{k.left}={k.right}" for k in pairs) if pairs else None
        # _Base sets use_enum_values, so stage.type is a plain str.
        return {"kind": str(stage.type), "detail": detail}
    return {"kind": str(stage.type), "detail": None}


def build_trace_view(
    trace: dict[str, Any], stages: dict[str, WorkflowStage], links: PanelLinks
) -> dict[str, Any]:
    chrono = list(reversed(trace["steps"]))
    end = trace["end"]
    truncated = not end["reached_origin"]

    nodes = [_build_node(i, chrono, stages, links, truncated) for i in range(len(chrono))]

    edges = [
        {"from": chrono[i]["stage_id"], "to": chrono[i + 1]["stage_id"],
         "from_step": i + 1, "to_step": i + 2, "data_row": chrono[i]["row"]}
        for i in range(len(chrono) - 1)
    ]

    return {
        "run_id": trace["run_id"],
        "start_stage": trace["start_stage"],
        "start_row": trace["start_row"],
        "nodes": nodes,
        "edges": edges,
        "upstream": {
            "truncated": truncated,
            "at_stage": end["at_stage"],
            "message": end["message"],
        },
    }


def read_walked_rows(view: dict[str, Any]) -> dict[str, int]:
    """Which row of each stage this walk stood on, so a pane can mark the path it took."""
    return {node["stage_id"]: node["row_ordinal"] for node in view["nodes"]}


def _list_reads_at(stages: dict[str, WorkflowStage], stage_id: str) -> frozenset[str]:
    """Empty where the run pinned a version this page cannot resolve."""
    workflow_stage = stages.get(stage_id)
    if workflow_stage is None:
        return frozenset()
    return frozenset(list_read_column_names(workflow_stage.stage))


def _build_node(
    i: int, chrono: list[dict[str, Any]], stages: dict[str, WorkflowStage],
    links: PanelLinks, truncated: bool,
) -> dict[str, Any]:
    step = chrono[i]
    # A crossing's parent is at another grain, so a cell diff would read as a change.
    parent = chrono[i - 1] if i and not step.get("sampled") else None
    diff = build_row_diff(
        step["row"],
        parent["row"] if parent else None,
        is_origin=(i == 0 and not truncated),
        read=_list_reads_at(stages, step["stage_id"]),
    )
    return {
        "step": i + 1,  # 1-based, chronological — so the story can say "step 4"
        "stage_id": step["stage_id"],
        "row_ordinal": step["row_ordinal"],
        # Ordinals address rows; a page names them. Nothing here counts on its own.
        "row_number": render_row_number(step["row_ordinal"]),
        "stage_type": step["stage_type"],
        # The slug stays — it is what the trace recorded — and the label beside it is
        # what the panel prints, so both surfaces name a type the same way.
        "stage_type_label": label_stage_type(step["stage_type"]),
        "origin": step["origin"],
        # The name alone; where it sat on disk is the manifest's business.
        "source_file": _source_filename(step),
        "source_row": step.get("source_row"),
        "source_row_number": _render_row_number_or_none(step.get("source_row")),
        "source_file_count": step.get("source_file_count"),
        "role": _role_of(i, len(chrono), truncated),
        "columns_new": step["columns_new"],
        "row": step["row"],
        "row_diff": row_diff_to_dict(diff),
        # What the row was compared against, named so the panel can state it
        # rather than leaving the reader to assume which frame the diff used.
        "base": None if parent is None else {
            "stage_id": parent["stage_id"], "row_ordinal": parent["row_ordinal"],
            "row_number": render_row_number(parent["row_ordinal"]),
        },
        "transform": _transform_of(stages.get(step["stage_id"])),
        # The step line's two sentences; app/web/stage_prose.py writes the second.
        "description": _describe_stage(stages.get(step["stage_id"])),
        "says": _say_the_step(stages.get(step["stage_id"])),
        # Straight from the walk, which knew the fan-in it sampled from here.
        "sampled": step.get("sampled"),
        "links": _links_of(links, step["stage_id"], step["row_ordinal"]),
        # Fan-in parents are NOT in `branches` — a row can have tens of
        # thousands, and `branches` is what the reader promotes one at a time.
        "branches": [_name_the_row(branch, links) for branch in _spine_branches(step)],
        "contributor_groups": [
            asdict(group) for group in _group_contributors(_contributions(step), links)
        ],
    }


def _describe_stage(workflow_stage: WorkflowStage | None) -> str | None:
    return None if workflow_stage is None else workflow_stage.stage.description


def _say_the_step(workflow_stage: WorkflowStage | None) -> str | None:
    return None if workflow_stage is None else say_what_a_stage_did(workflow_stage.stage)


def _name_the_row(entry: dict[str, Any], links: PanelLinks) -> dict[str, Any]:
    """A parent row as the page shows it: where it opens, and what it is called there."""
    ordinal = int(entry["row_ordinal"])
    return {**entry, "links": _links_of(links, entry["stage_id"], ordinal),
            "row_number": render_row_number(ordinal)}


def _render_row_number_or_none(ordinal: int | None) -> str | None:
    return None if ordinal is None else render_row_number(ordinal)


def _source_filename(step: dict[str, Any]) -> str | None:
    read_from = step.get("source_file")
    return PurePath(read_from).name if read_from else None


def _contributions(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in (step.get("branches") or [])
            if b.get("kind") == EdgeKind.contribution.value]


def _spine_branches(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in (step.get("branches") or [])
            if b.get("kind") != EdgeKind.contribution.value]


def _role_of(i: int, total: int, truncated: bool) -> str:
    if i == total - 1:
        return "claim"
    return "source" if i == 0 and not truncated else "step"


def _links_of(links: PanelLinks, stage_id: str, row_ordinal: int) -> dict[str, str | None]:
    return {
        "stage": links.stage_anchor(stage_id),
        "rows": links.stage_rows(stage_id),
        "trace": links.row_trace(stage_id, row_ordinal),
    }


def _group_contributors(
    contributions: list[dict[str, Any]], links: PanelLinks
) -> list[ContributorGroup]:
    by_key: dict[tuple[str, tuple[str, ...] | None], list[dict[str, Any]]] = {}
    # Grouped over the WHOLE set before anything is dropped, so a cohort's
    # `total` and the number of cohorts are both exact however many rows are
    # then linked. Bounding first would let a big cohort's tail hide a cohort of
    # its own.
    for parent in contributions:
        columns = parent.get("columns")
        key = (str(parent["stage_id"]), tuple(columns) if columns else None)
        by_key.setdefault(key, []).append(parent)
    return [
        _one_group(stage_id, columns, parents, links)
        for (stage_id, columns), parents in by_key.items()
    ]


def _one_group(
    stage_id: str, columns: tuple[str, ...] | None,
    parents: list[dict[str, Any]], links: PanelLinks,
) -> ContributorGroup:
    named = parents[:CONTRIBUTORS_NAMED] if len(parents) <= CONTRIBUTORS_NAMED else []
    return ContributorGroup(
        stage_id=stage_id,
        columns=list(columns) if columns else None,
        total=len(parents),
        linked=links.rows_link_covers(len(parents)),
        named=[_name_the_row(p, links) for p in named],
        rows_link=links.contributor_rows(
            stage_id,
            ordinals=[int(p["row_ordinal"]) for p in parents[:CONTRIBUTOR_ROWS_LINKED]]),
    )
