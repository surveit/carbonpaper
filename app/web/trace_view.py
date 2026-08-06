"""View-model for show-your-work: fold a linear trace (app.runtime.trace) plus the
compiled stages into the payload the template renders.

The payload is a graph (`nodes` + `edges`) even though v1 traces a single chain,
so real fan-in slots in without reshaping this contract.
"""
from __future__ import annotations

from typing import Any

from app.models import Stage
from app.models.stages.code import PythonFrameFunctionStage, PythonRowFunctionStage
from app.models.stages.input_data import InputDataStage
from app.models.stages.join import EnrichStage, ExpandStage
from app.models.stages.llm_transform import LLMTransformStage
from app.models.stages.starlark import StarlarkRowFunctionStage
from app.services.loader import resolve_function_code
from app.web.panel_links import AppPanelLinks
from app.web.trace_row_diff import build_row_diff, row_diff_to_dict


def _transform_of(stage: Stage | None) -> dict[str, Any]:
    """What the stage did: a `kind` the template styles on, plus a `detail` blob."""
    # `unknown` where the compiled stage is absent — the tracer needs only the
    # run dir, and the compiled DAG may not be loadable.
    if stage is None:
        return {"kind": "unknown", "detail": None}
    if isinstance(stage, InputDataStage):
        path = stage.connector.params.get("path")
        src = path or (stage.source.doc if stage.source else None)
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
    trace: dict[str, Any], stages: dict[str, Stage], links: AppPanelLinks
) -> dict[str, Any]:
    """Turn `trace` into the render payload: nodes chronological, claim last."""
    # Each node carries its row — as fields marked against the parent row the
    # walk came from — its transform, and any `branches`: parents the walk did
    # not follow, offered as promotable traces rather than expanded inline, so
    # the page stays one story. `upstream` folds the terminal stop reason onto
    # the earliest node, not its own step.
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


def _build_node(
    i: int, chrono: list[dict[str, Any]], stages: dict[str, Stage],
    links: AppPanelLinks, truncated: bool,
) -> dict[str, Any]:
    """One step as the page reads it: its row against its parent's, and where it links."""
    step = chrono[i]
    parent = chrono[i - 1] if i else None
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
        "origin": step["origin"],
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
        "branches": [
            {**branch, "links": _links_of(links, branch["stage_id"], branch["row_ordinal"])}
            for branch in (step.get("branches") or [])
        ],
    }


def _role_of(i: int, total: int, truncated: bool) -> str:
    """`claim` last, `source` first where the walk reached an origin, else `step`."""
    if i == total - 1:
        return "claim"
    return "source" if i == 0 and not truncated else "step"


def _links_of(links: AppPanelLinks, stage_id: str, row_ordinal: int) -> dict[str, str]:
    """Where one node points, in the app's own link vocabulary rather than a hand-built URL."""
    return {
        "stage": links.stage_anchor(stage_id),
        "rows": links.stage_rows(stage_id),
        "trace": links.row_trace(stage_id, row_ordinal),
    }
