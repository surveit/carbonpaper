"""View-model for show-your-work: fold a linear trace (from
`app.runtime.trace`) plus the compiled stages into the chronological
story/graph payload the template renders.

Two separations the design rests on:
  - a stage is a *transform* (a verb — the code it ran, the prompt it asked,
    the keys it joined on), taken from the compiled `Stage`;
  - an edge is *data* (the rows that flowed), taken from the run outputs the
    tracer already read.

The payload is a graph (`nodes` + `edges`) even though v1 traces a single
chain, so real fan-in (issue #58) slots in without reshaping this contract.
"""
from __future__ import annotations

from typing import Any

from app.core.paths import strip_directories
from app.models import Stage
from app.models.stage import StageType
from app.services.loader import resolve_function_code


def _transform_of(stage: Stage | None, input_identity: dict[str, Any] | None) -> dict[str, Any]:
    """What the stage did, for the node-detail panel: a `kind` the template
    styles on and a `detail` blob (code / prompt / keys / source). `unknown`
    when the compiled stage is absent (the tracer needs only the run dir; the
    compiled DAG may not be loadable)."""
    if input_identity is not None:
        # Only an input stage carries one, so it settles the kind on its own —
        # and it identifies the source even when the compiled stage is missing.
        return {"kind": "source", "detail": _describe_hashed_input(input_identity)}
    if stage is None:
        return {"kind": "unknown", "detail": None}
    # _Base sets use_enum_values, so stage.type is a plain str; compare by value.
    stage_type = str(stage.type)
    if stage_type == StageType.input_data.value:
        return {"kind": "source", "detail": _describe_unhashed_input(stage)}
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


def _describe_hashed_input(input_identity: dict[str, Any]) -> str:
    """Filename + content hash, never a path: the page ships to outside readers."""
    return (f"{input_identity['filename']} — sha256 {input_identity['sha256']} "
            f"({input_identity['bytes']} bytes)")


def _describe_unhashed_input(stage: Stage) -> str:
    """For a run whose manifest recorded no binding: names the file, never its
    directories, so the page still says which input without disclosing where."""
    path = stage.connector.params.get("path") if stage.connector else None
    named = strip_directories(str(path)) if path else None
    return named or (stage.source.doc if stage.source else None) or "originates the rows"


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
            "transform": _transform_of(stages.get(step["stage_id"]), step["input_identity"]),
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
