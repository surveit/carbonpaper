"""StageExample — one authored input→expected-output case for a python transform
(python_row_function / python_frame_function) — plus the shape checks the Stage
model runs when it carries examples.

An example is a claim about what the stage's function must do, authored from the
methodology, never produced by executing the stage's own code (that would assert
the code equals itself). The runner that holds the code to these claims is
app.runtime.examples; this module is only the data shape and its invariants.
Schema conformance of the rows needs dataframes, so it is checked by the runner,
not here.
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.models.schema import _Base

# The stage types whose handlers can execute an example.
EXAMPLE_STAGE_TYPES = frozenset({"python_row_function", "python_frame_function"})


class StageExample(_Base):
    """One case: `inputs` maps each of the stage's declared upstream ids to that
    input's rows; `expected` is the output rows those inputs must produce.
    `name` is the case's stable handle — what it pins down, e.g.
    "withdrawn_bill_maps_to_null"; `description` says why the case exists."""
    name: str
    description: Optional[str] = None
    inputs: dict[str, list[dict[str, Any]]]
    expected: list[dict[str, Any]]


def check_stage_examples(
    stage_type: str, input_ids: list[str], examples: list[StageExample]
) -> None:
    """Raise ValueError if `examples` are malformed for a stage of `stage_type`
    with the declared `input_ids`: examples belong on python transforms only,
    names are non-empty and unique, each example supplies exactly the declared
    inputs, and a python_row_function example is one row in → one row out (the
    type is 1:1 by construction, so an example claiming otherwise is wrong)."""
    if not examples:
        return
    if stage_type not in EXAMPLE_STAGE_TYPES:
        raise ValueError(
            f"examples are only supported on python transforms, not `{stage_type}`"
        )
    names = [example.name for example in examples]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"duplicate example name(s): {duplicates}")
    declared = set(input_ids)
    for example in examples:
        if not example.name.strip():
            raise ValueError("an example needs a non-empty name")
        if set(example.inputs) != declared:
            raise ValueError(
                f"example {example.name!r}: inputs keys {sorted(example.inputs)} "
                f"must be exactly the stage's declared inputs {sorted(declared)}"
            )
        if stage_type == "python_row_function":
            input_rows = next(iter(example.inputs.values()), [])
            if len(input_rows) != 1 or len(example.expected) != 1:
                raise ValueError(
                    f"example {example.name!r}: a python_row_function example is "
                    f"one row in → one row out (got {len(input_rows)} in, "
                    f"{len(example.expected)} out)"
                )
