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
from app.services.loader import resolve_function_code
from app.web.config import label_stage_type
from app.web.panel_links import (
    CONTRIBUTOR_ROWS_LINKED,
    CONTRIBUTORS_NAMED,
    PanelLinks,
    TracePath,
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


StoryKind = Literal["shown", "sampled", "branch", "contributor", "cohort"]


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
    trace: dict[str, Any], stages: dict[str, WorkflowStage], links: PanelLinks
) -> dict[str, Any]:
    chrono = list(reversed(trace["steps"]))
    end = trace["end"]
    truncated = not end["reached_origin"]
    path = _read_path(trace)
    paths = _build_path_per_node(chrono, path)

    nodes = [_build_node(i, chrono, stages, links, truncated, paths[i])
             for i in range(len(chrono))]

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
        "stories": [asdict(story) for story in _build_stories(nodes, links)],
        "upstream": {
            "truncated": truncated,
            "at_stage": end["at_stage"],
            "message": end["message"],
        },
    }


def _read_path(trace: dict[str, Any]) -> TracePath:
    """`steps` runs newest first, which is the order the walk consumed the choices in."""
    return TracePath(
        start=(trace["start_stage"], trace["start_row"]),
        sampled=tuple(
            (step["followed"]["stage_id"], step["followed"]["row_ordinal"])
            for step in trace["steps"] if step.get("followed")
        ),
    )


def _build_path_per_node(
    chrono: list[dict[str, Any]], path: TracePath
) -> list[TracePath]:
    """A pick at a fan-in REPLACES the row sampled there; the samples past it are dropped."""
    made, cut = 0, []
    for step in reversed(chrono):  # the walk's own order
        cut.append(TracePath(path.start, path.sampled[:made]))
        if step.get("followed"):
            made += 1
    return list(reversed(cut))


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
    links: PanelLinks, truncated: bool, path: TracePath,
) -> dict[str, Any]:
    step = chrono[i]
    groups = _group_contributors(_contributions(step), links, path)
    sampled = _mark_sampled_row(step)
    # Off `step`: an unmarked one-row fan-in still summarized, so still no diff.
    parent = chrono[i - 1] if i and not step.get("followed") else None
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
        "sampled": None if sampled is None else asdict(sampled),
        "contributor_groups": [asdict(group) for group in groups],
    }


@dataclass(frozen=True)
class SampledRow:
    """The row taken out of a fan-in, its 1-based place among them, and how many there were."""

    stage_id: str
    row_ordinal: int
    columns: list[str] | None
    at: int
    of: int


def _mark_sampled_row(step: dict[str, Any]) -> SampledRow | None:
    """None where the fan-in held ONE row: nothing was sampled, so there is nothing to mark."""
    followed = step.get("followed")
    merged = _contributions(step)
    if not followed or len(merged) < 2:
        return None
    return SampledRow(
        stage_id=followed["stage_id"],
        row_ordinal=followed["row_ordinal"],
        columns=followed["columns"],
        at=_find_place_among(merged, followed),
        of=len(merged),
    )


def _find_place_among(merged: list[dict[str, Any]], followed: dict[str, Any]) -> int:
    """1-based, so the reader counts rows the way the sentence beside it reads."""
    key = (followed["stage_id"], followed["row_ordinal"])
    return next(i for i, c in enumerate(merged, start=1)
                if (c["stage_id"], c["row_ordinal"]) == key)


def _cohort_key(parent: dict[str, Any]) -> tuple[str, tuple[str, ...] | None]:
    columns = parent.get("columns")
    return (str(parent["stage_id"]), tuple(columns) if columns else None)


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


def _links_of(
    links: PanelLinks, stage_id: str, row_ordinal: int, path: TracePath | None = None
) -> dict[str, str | None]:
    return {
        "stage": links.stage_anchor(stage_id),
        "rows": links.stage_rows(stage_id),
        "trace": links.row_trace(stage_id, row_ordinal),
        # This page, re-told with that row sampled at the fan-in it fed.
        "follow": (None if path is None
                   else links.follow_contributor(path, (stage_id, row_ordinal))),
    }


def _group_contributors(
    contributions: list[dict[str, Any]], links: PanelLinks, path: TracePath
) -> list[ContributorGroup]:
    by_key: dict[tuple[str, tuple[str, ...] | None], list[dict[str, Any]]] = {}
    # Grouped over the WHOLE set before anything is dropped, so a cohort's
    # `total` and the number of cohorts are both exact however many rows are
    # then linked. Bounding first would let a big cohort's tail hide a cohort of
    # its own.
    for parent in contributions:
        by_key.setdefault(_cohort_key(parent), []).append(parent)
    return [
        _one_group(stage_id, columns, parents, links, path)
        for (stage_id, columns), parents in by_key.items()
    ]


def _one_group(
    stage_id: str, columns: tuple[str, ...] | None,
    parents: list[dict[str, Any]], links: PanelLinks, path: TracePath,
) -> ContributorGroup:
    named = parents[:CONTRIBUTORS_NAMED] if len(parents) <= CONTRIBUTORS_NAMED else []
    return ContributorGroup(
        stage_id=stage_id,
        columns=list(columns) if columns else None,
        total=len(parents),
        linked=links.rows_link_covers(len(parents)),
        named=[{**p, "links": _links_of(links, p["stage_id"], int(p["row_ordinal"]),
                                        path=path)}
               for p in named],
        rows_link=links.contributor_rows(
            stage_id,
            ordinals=[int(p["row_ordinal"]) for p in parents[:CONTRIBUTOR_ROWS_LINKED]],
            path=path),
    )


def _build_stories(nodes: list[dict[str, Any]], links: PanelLinks) -> list[Story]:
    # A stage whose output frame the run never wrote traces no step at all.
    if not nodes:
        return []
    # Always first, so a row nothing else fed still reads as one story.
    first = nodes[0]
    shown = Story(
        kind="shown", stage_id=first["stage_id"], row_ordinal=first["row_ordinal"],
        step=1, rows=1, linked=0, columns=None, href=None,
    )
    return [shown, *(s for node in nodes for s in _find_alternatives(node, links))]


def _find_alternatives(node: dict[str, Any], links: PanelLinks) -> list[Story]:
    step = node["step"]
    sampled = node["sampled"]
    # A fan-in that merged ONE row offered nothing to pick, so it offers no entry.
    merged = sum(group["total"] for group in node["contributor_groups"])
    return [
        *([_build_sampled_story(sampled, step)] if sampled else []),
        *(_build_branch_story(branch, step) for branch in node["branches"]),
        *([] if merged < 2 else
          [s for group in node["contributor_groups"]
           for s in _build_fan_in_stories(group, step, sampled)]),
    ]


def _build_sampled_story(sampled: dict[str, Any], step: int) -> Story:
    """The reader is already on this one, so it carries no href — like the shown entry."""
    return Story(
        kind="sampled", stage_id=sampled["stage_id"],
        row_ordinal=sampled["row_ordinal"], step=step, rows=sampled["of"],
        linked=0, columns=sampled["columns"], href=None,
    )


def _build_branch_story(branch: dict[str, Any], step: int) -> Story:
    href = branch["links"]["trace"]
    return Story(
        kind="branch", stage_id=branch["stage_id"], row_ordinal=branch["row_ordinal"],
        step=step, rows=1, linked=1 if href else 0, columns=None, href=href,
    )


def _build_fan_in_stories(
    group: dict[str, Any], step: int, sampled: dict[str, Any] | None,
) -> list[Story]:
    if group["named"]:
        return [_build_contributor_story(parent, group, step)
                for parent in group["named"]
                if not _is_sampled(parent, sampled)]
    return [Story(
        kind="cohort", stage_id=group["stage_id"], row_ordinal=None, step=step,
        rows=group["total"], linked=group["linked"] if group["rows_link"] else 0,
        columns=group["columns"], href=group["rows_link"],
    )]


def _is_sampled(parent: dict[str, Any], sampled: dict[str, Any] | None) -> bool:
    """The sampled row has its own entry already; a second one would read as a fork."""
    return sampled is not None and (
        (parent["stage_id"], int(parent["row_ordinal"]))
        == (sampled["stage_id"], sampled["row_ordinal"])
    )


def _build_contributor_story(
    parent: dict[str, Any], group: dict[str, Any], step: int
) -> Story:
    # Crosses THIS page's path, rather than starting a fresh walk here.
    href = parent["links"]["follow"]
    return Story(
        kind="contributor", stage_id=parent["stage_id"],
        row_ordinal=int(parent["row_ordinal"]), step=step, rows=group["total"],
        linked=1 if href else 0, columns=group["columns"], href=href,
    )
