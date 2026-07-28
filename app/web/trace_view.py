"""View-model for show-your-work: fold a linear trace (app.runtime.trace) plus the
compiled stages into the payload the template renders.

The payload is a graph (`nodes` + `edges`) even though v1 traces a single chain,
so real fan-in slots in without reshaping this contract.
"""
from __future__ import annotations

from typing import Any

from app.models import Stage
from app.models.stage import StageType
from app.services.loader import resolve_function_code


def _transform_of(stage: Stage | None) -> dict[str, Any]:
    """What the stage did, for the node-detail panel: a `kind` the template
    styles on and a `detail` blob (code / prompt / keys / source). `unknown`
    when the compiled stage is absent (the tracer needs only the run dir; the
    compiled DAG may not be loadable)."""
    if stage is None:
        return {"kind": "unknown", "detail": None}
    # _Base sets use_enum_values, so stage.type is a plain str; compare by value.
    stage_type = str(stage.type)
    if stage_type == StageType.input_data.value:
        path = stage.connector.params.get("path") if stage.connector else None
        src = path or (stage.source.doc if stage.source else None)
        return {"kind": "source", "detail": src or "originates the rows"}
    if stage_type in (StageType.python_row_function.value, StageType.python_frame_function.value):
        # Full source: the whole module file for a module ref, the inline code
        # for an inline ref — never a partial snippet or a bare reference.
        return {"kind": "python", "detail": resolve_function_code(stage)}
    if stage_type == StageType.llm_transform.value:
        llm_detail = (
            {"instructions": stage.llm.prompt_instructions, "data_template": stage.llm.prompt_data_template}
            if stage.llm else None
        )
        return {"kind": "llm", "detail": llm_detail}
    if stage_type == StageType.join_.value:
        pairs = (stage.join.keys or stage.join.on) if stage.join else None
        detail = ", ".join(f"{k.left}={k.right}" for k in pairs) if pairs else None
        return {"kind": "join", "detail": detail}
    return {"kind": stage_type, "detail": None}


def build_trace_view(trace: dict[str, Any], stages: dict[str, Stage]) -> dict[str, Any]:
    """Turn `trace` (the dict from `trace_to_dict`) into the render payload.

    Nodes run chronologically (source/stop first, claim last). Each node
    carries its row, the columns new at that stage, and its transform detail.
    Edges connect consecutive nodes carrying the earlier node's row (the data
    that flowed forward). `upstream` records whether the walk stopped short of
    an origin and why (the terminal reason, folded onto the earliest node
    rather than shown as its own step).
    """
    chrono = list(reversed(trace["steps"]))
    end = trace["end"]
    truncated = not end["reached_origin"]

    nodes: list[dict[str, Any]] = []
    for i, step in enumerate(chrono):
        is_claim = i == len(chrono) - 1
        is_first = i == 0
        if is_claim:
            role = "claim"
        elif is_first and not truncated:
            role = "source"
        else:
            role = "step"
        nodes.append({
            "step": i + 1,  # 1-based, chronological — so the story can say "step 4"
            "stage_id": step["stage_id"],
            "row_ordinal": step["row_ordinal"],  # for loading the row-trimmed panel
            "stage_type": step["stage_type"],
            "origin": step["origin"],
            "role": role,
            "columns_new": step["columns_new"],
            "row": step["row"],
            "transform": _transform_of(stages.get(step["stage_id"])),
        })

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
