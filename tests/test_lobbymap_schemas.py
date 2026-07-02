"""The lobbymap data model + eval specs validate against the models contract.

Doubles as the exclusive_arcs (XOR foreign key) test suite: lobbymap is the
motivating case — a row scores a company XOR an influencer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app import models as m
from app.models.eval import EvalSpec, build_ground_truth_schema, validate_eval_spec

LOBBYMAP = Path(__file__).resolve().parent.parent / "examples" / "lobbymap"


def _load_yaml_docs(directory: Path, pattern: str) -> list[dict]:
    docs: list[dict] = []
    for f in sorted(directory.glob(pattern)):
        with f.open(encoding="utf-8") as fh:
            docs += [d for d in yaml.safe_load_all(fh) if d]
    return docs


def _generation_schemas() -> list[dict]:
    return _load_yaml_docs(LOBBYMAP / "schemas", "*.yaml")


# ── The data model itself ─────────────────────────────────────────────────────

def test_lobbymap_generation_model_validates():
    docs = _generation_schemas()
    assert len(docs) == 8, "expected 8 generation schemas"
    lib = m.parse_schema_library(docs)
    kinds = {s.name: s.kind.value for s in lib.schemas}
    assert kinds["benchmark"] == "reference"
    assert kinds["document"] == "input"
    assert kinds["cell_score"] == "computed"
    assert not any(k == "ground_truth" for k in kinds.values()), \
        "ground truth must not leak into the generation data model"


def test_lobbymap_xor_arcs_present():
    lib = m.parse_schema_library(_generation_schemas())
    by_name = {s.name: s for s in lib.schemas}
    for name in ("scored_evidence", "cell_score"):
        assert by_name[name].exclusive_arcs == [["company_id", "influencer_id"]]


# ── exclusive_arcs contract ───────────────────────────────────────────────────

def test_exclusive_arc_requires_declared_columns():
    with pytest.raises(ValidationError, match="not declared"):
        m.NamedSchema.model_validate(
            {"name": "x", "kind": "computed", "title": "X",
             "columns": [{"name": "a", "nullable": True}],
             "exclusive_arcs": [["a", "ghost"]]})


def test_exclusive_arc_requires_nullable_columns():
    with pytest.raises(ValidationError, match="must be nullable"):
        m.NamedSchema.model_validate(
            {"name": "x", "kind": "computed", "title": "X",
             "columns": [{"name": "a", "nullable": False}, {"name": "b", "nullable": True}],
             "exclusive_arcs": [["a", "b"]]})


def test_exclusive_arc_requires_two_columns():
    with pytest.raises(ValidationError, match=">= 2"):
        m.NamedSchema.model_validate(
            {"name": "x", "kind": "computed", "title": "X",
             "columns": [{"name": "a", "nullable": True}],
             "exclusive_arcs": [["a"]]})


# ── Eval specs vs the generation model ────────────────────────────────────────

def _eval_specs() -> list[dict]:
    return _load_yaml_docs(LOBBYMAP / "eval", "*.eval.yaml")


def test_lobbymap_eval_specs_consistent_with_generation():
    gen_by_name = {d["name"]: d for d in _generation_schemas()}
    specs = _eval_specs()
    assert {s["name"] for s in specs} == {"gt_cell_score", "gt_scored_evidence"}
    for spec in specs:
        assert validate_eval_spec(spec, gen_by_name) == []


def test_ground_truth_inherits_xor_arc():
    gen = {s.name: s for s in m.parse_schema_library(_generation_schemas()).schemas}
    spec = next(EvalSpec.model_validate(s) for s in _eval_specs()
                if s["name"] == "gt_cell_score")
    gt = build_ground_truth_schema(spec, gen["cell_score"])
    assert gt.kind is m.SchemaKind.ground_truth
    assert gt.exclusive_arcs == [["company_id", "influencer_id"]]
    gt_cols = {c.name for c in gt.columns}
    assert {"company_id", "influencer_id", "query_id", "source_id", "status", "score"} <= gt_cols
    assert "evidence_url" in gt_cols  # the eval-only provenance column
