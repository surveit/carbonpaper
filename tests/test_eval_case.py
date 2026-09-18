import json

import pytest

from app.core.llm.options import LLMModel
from app.evals.case import read_case
from app.evals.errors import CaseInvalid


def _write_case(case_dir, payload):
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case.json").write_text(json.dumps(payload), encoding="utf-8")


def _payload(**overrides):
    return {"output_slug": "grant-total", "claim_context": {}, "claim_text": "Grants came to 5.",
            "model": "claude-sonnet-5", "sources": [], "expected_outputs": [], **overrides}


def test_a_case_reads_what_it_takes_to_submit_the_claim(tmp_path):
    _write_case(tmp_path / "c", _payload(
        claim_context={"year": 2024},
        sources=[{"path": "sources/a.csv", "sha256": "abc"}],
        expected_outputs=["the figure is a share, not a count"]))

    case = read_case(tmp_path / "c")

    assert case.output_slug == "grant-total"
    assert case.claim_context == {"year": 2024}
    assert case.claim_text == "Grants came to 5."
    assert case.model == LLMModel.claude_sonnet_5
    assert [s.path for s in case.sources] == ["sources/a.csv"]


def test_a_case_naming_no_output_to_claim_is_refused(tmp_path):
    payload = _payload()
    del payload["output_slug"]
    _write_case(tmp_path / "c", payload)

    with pytest.raises(CaseInvalid) as refusal:
        read_case(tmp_path / "c")
    assert "output_slug" in str(refusal.value)


def test_a_case_with_no_file_is_refused_by_name(tmp_path):
    with pytest.raises(CaseInvalid) as refusal:
        read_case(tmp_path / "missing")
    assert "missing" in str(refusal.value)


def test_an_unversioned_model_alias_is_refused(tmp_path):
    _write_case(tmp_path / "c", _payload(model="sonnet"))
    with pytest.raises(CaseInvalid):
        read_case(tmp_path / "c")
