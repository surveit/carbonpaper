"""View-model for show-your-work: fold a linear trace (app.runtime.trace) plus the
compiled stages into the payload the template renders.

The payload is a graph (`nodes` + `edges`) even though v1 traces a single chain,
so real fan-in slots in without reshaping this contract.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePath
from typing import Any, Literal

from app.models import WorkflowStage
from app.models.stages.code import PythonFrameFunctionStage, PythonRowFunctionStage
from app.models.stages.input_data import InputDataStage
from app.models.stages.join import EnrichStage, ExpandStage
from app.models.stages.llm_transform import LLMTransformStage
from app.models.stages.starlark import StarlarkRowFunctionStage
from app.runtime.lineage import EdgeKind
from app.runtime.trace import TraceVia
from app.services.loader import resolve_function_code
from app.web.config import label_stage_type
from app.web.panel_links import (
    CONTRIBUTOR_ROWS_LINKED,
    CONTRIBUTORS_NAMED,
    PanelLinks,
    TracePageQuery,
)
from app.web.trace_row_diff import build_row_diff, render_cell, row_diff_to_dict

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


StoryKind = Literal["shown", "branch", "contributor", "cohort", "opened_on"]


@dataclass(frozen=True)
class Story:
    """One path this row's ancestry can be told down."""

    kind: StoryKind
    stage_id: str
    # None for a cohort — no single row of it speaks for the others.
    row_ordinal: int | None
    # Step of the shown path this one parts from.
    step: int
    # How many rows this entry stands for; 1 for a lone parent.
    rows: int
    # How many of them `href` opens — 0 where there is no href.
    linked: int
    columns: list[str] | None
    href: str | None


@dataclass(frozen=True)
class CitedCell:
    column: str
    value: str
    # The authored description, or a plain statement of why there is none.
    tip: str


def _transform_of(workflow_stage: WorkflowStage | None) -> dict[str, Any]:
    if workflow_stage is None:
        return {"kind": "unknown", "detail": None}
    stage = workflow_stage.stage
    if isinstance(stage, InputDataStage):
        named = ", ".join(stage.connector.params.paths)
        src = named or (stage.source.doc if stage.source else None)
        return {"kind": "source", "detail": src or "originates the rows"}
    if isinstance(stage, (PythonRowFunctionStage, PythonFrameFunctionStage)):
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
    trace: dict[str, Any], stages: dict[str, WorkflowStage], links: PanelLinks,
    query: TracePageQuery = TracePageQuery(),
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
        "switches_path": links.switches_path,
        "stories": [
            asdict(story) for story in _build_stories(
                nodes, links, trace["start_stage"], int(trace["start_row"]), query)
        ],
        "upstream": {
            "truncated": truncated,
            "at_stage": end["at_stage"],
            "message": end["message"],
        },
    }


def find_cited_cell(
    view: dict[str, Any], workflow_stage: WorkflowStage | None, column: str
) -> CitedCell | None:
    """None where the traced row carries no such column."""
    row = view["nodes"][-1]["row"]
    if column not in row:
        return None
    return CitedCell(
        column=column,
        value=render_cell(row[column]),
        tip=_describe_column(view["start_stage"], workflow_stage, column),
    )


def _describe_column(
    stage_id: str, workflow_stage: WorkflowStage | None, column: str
) -> str:
    if workflow_stage is None:
        return f"The version this run pinned is unreadable, so nothing declares {column} here."
    schema = workflow_stage.output_schema
    declared = schema.column_for_name(column) if schema else None
    if declared is None:
        return f"The version this run pinned declares no {column} on {stage_id}."
    if declared.description:
        return declared.description
    nullability = "null allowed" if declared.nullable else "not null"
    return (f"Declared {declared.type}, {nullability}. "
            f"No description was authored for this column.")


def _build_node(
    i: int, chrono: list[dict[str, Any]], stages: dict[str, WorkflowStage],
    links: PanelLinks, truncated: bool,
) -> dict[str, Any]:
    step = chrono[i]
    parent = chrono[i - 1] if i else None
    # A row it summarizes is one of many, never the row it was made from.
    if parent is not None and _summarizes(step, parent):
        parent = None
    diff = build_row_diff(
        step["row"],
        parent["row"] if parent else None,
        is_origin=(i == 0 and not truncated),
    )
    return {
        "step": i + 1,  # 1-based, chronological — so the story can say "step 4"
        "stage_id": step["stage_id"],
        "row_ordinal": step["row_ordinal"],
        "stage_type": step["stage_type"],
        # The slug stays — it is what the trace recorded — and the label beside it is
        # what the panel prints, so both surfaces name a type the same way.
        "stage_type_label": label_stage_type(step["stage_type"]),
        "origin": step["origin"],
        # The name alone; where it sat on disk is the manifest's business.
        "source_file": _source_filename(step),
        "source_row": step.get("source_row"),
        "source_file_count": step.get("source_file_count"),
        "role": _role_of(i, len(chrono), truncated),
        "columns_new": step["columns_new"],
        "row": step["row"],
        "row_diff": row_diff_to_dict(diff),
        # What the row was compared against, named so the panel can state it
        # rather than leaving the reader to assume which frame the diff used.
        "base": None if parent is None else {
            "stage_id": parent["stage_id"], "row_ordinal": parent["row_ordinal"],
        },
        "transform": _transform_of(stages.get(step["stage_id"])),
        "links": _links_of(links, step["stage_id"], step["row_ordinal"]),
        # Fan-in parents are NOT in `branches` — a row can have tens of
        # thousands, and `branches` is what the reader promotes one at a time.
        "branches": [
            {**branch, "links": _links_of(links, branch["stage_id"], branch["row_ordinal"])}
            for branch in _spine_branches(step)
        ],
        "contributor_groups": [
            asdict(group) for group in _group_contributors(_contributions(step), links)
        ],
    }


def _source_filename(step: dict[str, Any]) -> str | None:
    read_from = step.get("source_file")
    return PurePath(read_from).name if read_from else None


def _summarizes(step: dict[str, Any], parent: dict[str, Any]) -> bool:
    return any(c["stage_id"] == parent["stage_id"] and c["row_ordinal"] == parent["row_ordinal"]
               for c in _contributions(step))


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
        named=[{**p, "links": _links_of(links, p["stage_id"], int(p["row_ordinal"]))}
               for p in named],
        rows_link=links.contributor_rows(
            stage_id,
            ordinals=[int(p["row_ordinal"]) for p in parents[:CONTRIBUTOR_ROWS_LINKED]]),
    )


@dataclass(frozen=True)
class PathDestination:
    """The row every path on the page ends at, and the parents already routed through."""

    links: PanelLinks
    stage_id: str
    row: int
    query: TracePageQuery

    def build_link_through(self, step: int, stage_id: str, row_ordinal: int) -> str | None:
        return self.links.path_via(
            self.stage_id, self.row, self.query,
            TraceVia(step=step, stage_id=stage_id, row_ordinal=row_ordinal),
        )


def _build_stories(
    nodes: list[dict[str, Any]], links: PanelLinks,
    start_stage: str, start_row: int, query: TracePageQuery,
) -> list[Story]:
    # A stage whose output frame the run never wrote traces no step at all.
    if not nodes:
        return []
    # Always first, so a row nothing else fed still reads as one story.
    first = nodes[0]
    shown = Story(
        kind="shown", stage_id=first["stage_id"], row_ordinal=first["row_ordinal"],
        step=1, rows=1, linked=0, columns=None, href=None,
    )
    dest = PathDestination(links, start_stage, start_row, query)
    on_path = {(node["stage_id"], node["row_ordinal"]) for node in nodes}
    alternatives = [
        story for node in nodes for story in _find_alternatives(node, dest)
        if (story.stage_id, story.row_ordinal) not in on_path
    ]
    return [shown, *alternatives, *_build_opened_on_story(dest)]


def _build_opened_on_story(dest: PathDestination) -> list[Story]:
    """The path before any via — reachable only from a page that routed through one."""
    if not dest.query.vias:
        return []
    return [Story(
        kind="opened_on", stage_id=dest.stage_id, row_ordinal=dest.row,
        step=1, rows=1, linked=1, columns=None,
        href=dest.links.path_reset(dest.stage_id, dest.row, dest.query),
    )]


def _find_alternatives(node: dict[str, Any], dest: PathDestination) -> list[Story]:
    step = node["step"]
    return [
        *(_build_branch_story(branch, step, dest) for branch in node["branches"]),
        *(s for group in node["contributor_groups"]
          for s in _build_fan_in_stories(group, step, dest)),
    ]


def _build_branch_story(branch: dict[str, Any], step: int, dest: PathDestination) -> Story:
    href = dest.build_link_through(step, branch["stage_id"], int(branch["row_ordinal"]))
    return Story(
        kind="branch", stage_id=branch["stage_id"], row_ordinal=branch["row_ordinal"],
        step=step, rows=1, linked=1 if href else 0, columns=None, href=href,
    )


def _build_fan_in_stories(
    group: dict[str, Any], step: int, dest: PathDestination
) -> list[Story]:
    if group["named"]:
        return [
            _build_contributor_story(parent, group, step, dest) for parent in group["named"]
        ]
    return [Story(
        kind="cohort", stage_id=group["stage_id"], row_ordinal=None, step=step,
        rows=group["total"], linked=group["linked"] if group["rows_link"] else 0,
        columns=group["columns"], href=group["rows_link"],
    )]


def _build_contributor_story(
    parent: dict[str, Any], group: dict[str, Any], step: int, dest: PathDestination
) -> Story:
    href = dest.build_link_through(step, parent["stage_id"], int(parent["row_ordinal"]))
    return Story(
        kind="contributor", stage_id=parent["stage_id"],
        row_ordinal=int(parent["row_ordinal"]), step=step, rows=group["total"],
        linked=1 if href else 0, columns=group["columns"], href=href,
    )
