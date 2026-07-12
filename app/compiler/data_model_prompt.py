"""The data-model authoring system prompt: describe a methodology's tables as a
SchemaLibrary (a set of NamedSchemas).

The emit shape is DERIVED from the Pydantic model — `SchemaLibrary.model_json_schema()`
with presentational noise (titles/defaults) stripped — so the prompt cannot drift from
what validation accepts, and the agent's output is validated straight back into
`SchemaLibrary`. Exposing the model's full field set is deliberate: the generated model
must not diverge from `NamedSchema`, so the workflow can consume it downstream.
"""
from __future__ import annotations

import json
from typing import Any

from app.models.named_schemas import SchemaLibrary


def _shape_reference() -> str:
    """The JSON shape the agent must emit — SchemaLibrary's own JSON schema, with
    presentational keys stripped so the prompt and the validator describe exactly the
    same thing."""
    return json.dumps(_strip_noise(SchemaLibrary.model_json_schema()), indent=2)


def _strip_noise(node: Any) -> Any:
    """Drop keys that add no information for the agent (titles, defaults, the
    extra-fields flag), recursing through the schema tree."""
    if isinstance(node, dict):
        return {
            key: _strip_noise(value)
            for key, value in node.items()
            if key not in {"title", "default", "additionalProperties"}
        }
    if isinstance(node, list):
        return [_strip_noise(value) for value in node]
    return node


DATA_MODEL_SYSTEM_PROMPT = f"""\
You are a METHODOLOGY COMPILER. Given a research transcript or a prose description of an
investigation, describe its DATA MODEL — the tables the methodology operates on — as a
SchemaLibrary: a set of NAMED SCHEMAS.

Emit ONE JSON object only (optionally inside a ```json fence), conforming to this schema:

{_shape_reference()}

# What a good data model is
- The schemas exist to TYPE and VALIDATE the objects that flow through a repeatable
  workflow which re-runs this methodology. Emit the FEWEST, SIMPLEST tables that capture
  the core NOUNS of the methodology — not every incidental or intermediate table. A few
  well-chosen schemas beat many granular ones.
- Choose each table's `kind` truthfully: `reference` = must be SOURCED, not computed (a
  dimension / lookup / benchmark); `input` = raw data fetched into the pipeline;
  `computed` = produced by a later pipeline stage; `ground_truth` = external truth used
  only to evaluate the pipeline.
- Wire foreign keys with a column's `references` ("<schema>" or "<schema>.<column>")
  wherever one table points at another, so the data model is a connected graph.
- NEVER fabricate data values, URLs, or numbers — encode STRUCTURE only; record genuine
  ambiguity in a schema's or a column's `description`."""
