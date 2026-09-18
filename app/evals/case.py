"""A captured eval case on disk: its claim, the model under test, and its source files."""
from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from app.core.json_types import JsonDict
from app.core.llm.options import LLMModel
from app.evals.errors import CaseInvalid
from app.models.schema import _Base

CASE_FILE = "case.json"


class CaseSource(_Base):
    path: str
    sha256: str


class Case(_Base):
    # What it takes to SUBMIT the claim: an imported archive carries no claims.
    output_slug: str
    claim_context: JsonDict
    claim_text: str
    model: LLMModel
    sources: list[CaseSource]
    expected_outputs: list[str]


def read_case(case_dir: Path) -> Case:
    path = case_dir / CASE_FILE
    try:
        return Case.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as absent:
        raise CaseInvalid(f"{path} does not exist") from absent
    except ValidationError as invalid:
        raise CaseInvalid(f"{path} is not a case: {invalid}") from invalid
