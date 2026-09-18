import json
import pytest

from app.core.llm.options import LLMModel
from app.evals.case import Case, CaseInvalid, read_case


def _write_case(case_dir, payload):
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_case_reads_its_claim_model_and_sources(tmp_path):
    _write_case(tmp_path / "c", {
        "claim_id": "c1", "model": "claude-sonnet-5",
        "sources": [{"path": "sources/a.csv", "sha256": "abc"}],
        "expected_outputs": ["the figure is a share, not a count"]})
    case = read_case(tmp_path / "c")
    assert case.claim_id == "c1"
    assert case.model == LLMModel.claude_sonnet_5
    assert [s.path for s in case.sources] == ["sources/a.csv"]


def test_a_case_with_no_file_is_refused_by_name(tmp_path):
    with pytest.raises(CaseInvalid) as refusal:
        read_case(tmp_path / "missing")
    assert "missing" in str(refusal.value)


def test_an_unversioned_model_alias_is_refused(tmp_path):
    _write_case(tmp_path / "c", {
        "claim_id": "c1", "model": "sonnet", "sources": [], "expected_outputs": []})
    with pytest.raises(CaseInvalid):
        read_case(tmp_path / "c")
