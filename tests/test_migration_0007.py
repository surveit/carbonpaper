"""Every stage of a document gets its signature — not just the first one, which
is what 0006 shipped and what 0007 exists to repair."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic/versions"
_REVISIONS = {"0006": _VERSIONS / "0006_synthesize_stage_signatures.py",
              "0007": _VERSIONS / "0007_finish_stage_signatures.py"}


def _load_revision(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"_rev_{name}", _REVISIONS[name])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(stage_id: str) -> dict[str, Any]:
    columns = [{"name": "id", "type": "str", "nullable": True}]
    return {"id": stage_id, "description": stage_id, "type": "python_frame_function",
            "inputs": [{"id": "src", "schema": {"columns": list(columns)}}],
            "function": {"kind": "inline", "summary": "Passes rows through.",
                         "code": "def transform(df):\n    return df"},
            "output_schema": {"columns": list(columns)}}


def _document() -> dict[str, Any]:
    return {"stages": [_stage("a"), _stage("b"), _stage("c")]}


def _stale_stage_ids(document: dict[str, Any]) -> list[str]:
    return [s["id"] for s in document["stages"] if "output_schema" in s]


def _dropping_stage() -> dict[str, Any]:
    """A row function whose stored outer dropped an anchor column."""
    return {"id": "gate", "description": "Gate", "type": "python_row_function",
            "inputs": [{"id": "src", "schema": {"columns": [
                {"name": "id", "type": "str", "nullable": True},
                {"name": "scratch", "type": "str", "nullable": True}]}}],
            "function": {"kind": "inline", "summary": "Passes rows through.",
                         "code": "def transform(row):\n    return row"},
            "output_schema": {"columns": [{"name": "id", "type": "str", "nullable": True}]}}


def test_every_stage_is_migrated_not_only_the_first():
    document = _document()
    assert _load_revision("0006")._add_signatures(document) is True
    assert _stale_stage_ids(document) == []
    assert all("signature" in s for s in document["stages"])


def test_a_dropping_stage_is_widened_and_reported():
    document = {"stages": [_dropping_stage()]}
    widened: list[str] = []
    assert _load_revision("0007")._add_signatures(document, "proj/v1", widened) is True
    assert widened == ["proj/v1 :: gate (python_row_function) regains ['scratch']"]

    from app.models import parse_stage
    stage = parse_stage(document["stages"][0])
    # The dropped column flows: it is back in the resolved output.
    assert [c.name for c in stage.resolve_output_schema().columns] == ["id", "scratch"]


def test_a_determinable_stage_is_not_reported_as_widened():
    document = _document()
    widened: list[str] = []
    _load_revision("0007")._add_signatures(document, "proj/v1", widened)
    assert widened == []


def test_the_synthesis_still_refuses_a_drop_by_default():
    from tools.stage_signatures import SignatureUndeterminable, add_signature
    import pytest
    with pytest.raises(SignatureUndeterminable):
        add_signature(_dropping_stage())


def test_0007_finishes_a_document_left_half_migrated():
    # The exact state 0006's short-circuit left in the store: stage 0 done, rest stale.
    document = _document()
    from tools.stage_signatures import add_signature
    add_signature(document["stages"][0])
    assert _stale_stage_ids(document) == ["b", "c"]

    assert _load_revision("0007")._add_signatures(document, "proj/v1", []) is True
    assert _stale_stage_ids(document) == []


def test_0007_leaves_an_already_complete_document_untouched():
    document = _document()
    rev = _load_revision("0007")
    rev._add_signatures(document, "proj/v1", [])
    assert rev._add_signatures(document, "proj/v1", []) is False


def _queueless_queue_stage() -> dict[str, Any]:
    """A queue stage whose queue block does not read — nothing determines its adds."""
    return {"id": "gate", "description": "Gate", "type": "human_review_queue",
            "inputs": [{"id": "src", "schema": {"columns": [
                {"name": "id", "type": "str", "nullable": True}]}}],
            "queue": {"filter": "id != ''"},
            "output_schema": {"columns": [{"name": "id", "type": "str", "nullable": True}]}}


def test_an_unreadable_queue_block_is_refused_not_guessed():
    from tools.stage_signatures import SignatureUndeterminable, add_signature
    import pytest
    with pytest.raises(SignatureUndeterminable, match="queue block does not read"):
        add_signature(_queueless_queue_stage(), allow_drops=True)
