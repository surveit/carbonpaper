"""DAG-level contract: a methodology is a list of validated stages plus the
cross-stage checks (unique ids, inputs resolve, acyclic).

The graph checks are plain functions so they can be tested on their own and read
without wading through a validator.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError, model_validator

from app.models.schema import _Base, format_errors
from app.models.stage import Stage


def check_unique_ids(stages: list[Stage]) -> None:
    ids = [s.id for s in stages]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate stage id(s): {dupes}")


def check_inputs_resolve(stages: list[Stage]) -> None:
    ids = {s.id for s in stages}
    for s in stages:
        for upstream in s.inputs:
            if upstream not in ids:
                raise ValueError(f"`{s.id}`: input `{upstream}` references no stage")


def detect_cycle(stages: list[Stage]) -> None:
    edges = {s.id: list(s.inputs) for s in stages}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in edges}

    def visit(node: str, path: list[str]) -> None:
        color[node] = GRAY
        for nxt in edges.get(node, []):
            if color.get(nxt) == GRAY:
                raise ValueError(f"cycle detected: {' -> '.join(path + [node, nxt])}")
            if color.get(nxt) == WHITE:
                visit(nxt, path + [node])
        color[node] = BLACK

    for sid in edges:
        if color[sid] == WHITE:
            visit(sid, [])


class Methodology(_Base):
    """A whole DAG: validated stages with unique ids, resolvable inputs, acyclic."""
    stages: list[Stage]

    @model_validator(mode="after")
    def _validate_dag(self) -> "Methodology":
        check_unique_ids(self.stages)
        check_inputs_resolve(self.stages)
        detect_cycle(self.stages)
        return self


def parse_methodology(stages: list[dict[str, Any]]) -> Methodology:
    """Parse + validate a list of stage dicts. Raises ValidationError if invalid."""
    return Methodology(stages=list(stages))


def validate_methodology(stages: list[dict[str, Any]]) -> list[str]:
    """Non-fatal: return human-readable issues ([] means valid). For the UI/compiler,
    which want to show problems rather than crash."""
    try:
        Methodology(stages=list(stages))
        return []
    except ValidationError as err:
        return format_errors(err)
