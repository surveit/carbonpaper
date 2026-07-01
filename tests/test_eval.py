"""Tests for app/models/eval.py — eval as a separate, derived overlay."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m

GEN = {
    "name": "org_score", "kind": "computed", "title": "Org score",
    "columns": [{"name": "company_id", "type": "str"}, {"name": "score", "type": "float"}],
    "primary_key": ["company_id"],
}


def test_eval_spec_requires_metrics():
    with pytest.raises(ValidationError):
        m.EvalSpec.model_validate({"name": "e", "evaluates": "org_score", "metrics": []})


def test_eval_spec_ok():
    e = m.EvalSpec.model_validate(
        {"name": "e", "evaluates": "org_score", "metrics": ["mae"], "mirror_columns": ["company_id", "score"]}
    )
    assert e.evaluates == "org_score"


def test_build_ground_truth_mirrors_generation():
    gen = m.NamedSchema.model_validate(GEN)
    spec = m.EvalSpec.model_validate(
        {"name": "org_score_truth", "evaluates": "org_score", "metrics": ["mae"],
         "mirror_columns": ["company_id", "score"]}
    )
    gt = m.build_ground_truth_schema(spec, gen)
    assert gt.kind is m.SchemaKind.ground_truth
    assert [c.name for c in gt.columns] == ["company_id", "score"]
    assert gt.primary_key == ["company_id"]
    assert gt.title == "Org score"   # derived from the generation schema


def test_build_ground_truth_appends_extra_and_drops_unmirrored_pk():
    gen = m.NamedSchema.model_validate(GEN)
    spec = m.EvalSpec.model_validate(
        {"name": "t", "evaluates": "org_score", "metrics": ["mae"],
         "mirror_columns": ["score"], "extra_columns": [{"name": "reviewer", "type": "str"}]}
    )
    gt = m.build_ground_truth_schema(spec, gen)
    names = [c.name for c in gt.columns]
    assert "score" in names and "reviewer" in names
    assert gt.primary_key is None   # company_id wasn't mirrored


def test_validate_eval_spec_unknown_target():
    issues = m.validate_eval_spec({"name": "e", "evaluates": "ghost", "metrics": ["mae"]}, {"org_score": GEN})
    assert any("unknown generation schema" in i for i in issues)


def test_validate_eval_spec_mirror_not_a_column():
    issues = m.validate_eval_spec(
        {"name": "e", "evaluates": "org_score", "metrics": ["mae"], "mirror_columns": ["nope"]},
        {"org_score": GEN},
    )
    assert any("not a column" in i for i in issues)


def test_validate_eval_spec_extra_collision():
    issues = m.validate_eval_spec(
        {"name": "e", "evaluates": "org_score", "metrics": ["mae"], "extra_columns": [{"name": "score", "type": "float"}]},
        {"org_score": GEN},
    )
    assert any("collides" in i for i in issues)


def test_validate_eval_spec_clean():
    assert m.validate_eval_spec(
        {"name": "e", "evaluates": "org_score", "metrics": ["mae"], "mirror_columns": ["company_id", "score"]},
        {"org_score": GEN},
    ) == []
