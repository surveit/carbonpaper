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
from app.services.loader import resolve_function_code


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


def build_trace_view(trace: dict[str, Any], stages: dict[str, Stage]) -> dict[str, Any]:
    """Turn `trace` into the render payload: nodes chronological, claim last."""
    # Each node carries its row, the columns new at that stage, its transform,
    # and any `branches` — parents the walk did not follow, offered as promotable
    # traces rather than expanded inline, so the page stays one story. `upstream`
    # folds the terminal stop reason onto the earliest node, not its own step.
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
            # Recorded parents this walk did not follow (the other side of a
            # join). Each is a trace of its own the reader can promote onto the
            # spine; the template builds the link from the run it is already on.
            "branches": step.get("branches") or [],
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
