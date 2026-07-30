"""Tests for app.services.methodology — adjudicated stage-test disputes flow back
into the methodology prose as reviewable amendments (issue #153).

Covers: recording an adjudication as a proposed amendment; the rendered prose
carrying both the ambiguity and the resolution; the round-trip through disk;
publishing appending the prose to the canonical document (and only then); the
fail-loudly guards (no document, double publish, publishing a rejected amendment);
and rejection leaving the document untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import methodology
from app.services.methodology import Adjudication


def _adj(**over: object) -> Adjudication:
    base: dict[str, object] = {
        "stage_id": "map_status",
        "stage_name": "Map bill status",
        "ambiguity": "A withdrawn bill's status was left unpinned by the tests.",
        "resolution": "A withdrawn bill maps to null, not to its last active status.",
    }
    base.update(over)
    return Adjudication(**base)  # type: ignore[arg-type]


def _write_doc(pdir: Path, text: str = "# Methodology\n\nThe original prose.\n") -> Path:
    pdir.mkdir(parents=True, exist_ok=True)
    doc = pdir / "document.md"
    doc.write_text(text, encoding="utf-8")
    return doc


def test_record_adjudication_creates_proposed_amendment(tmp_path: Path) -> None:
    _write_doc(tmp_path)
    amendment = methodology.record_adjudication(tmp_path, _adj(), reviewer="alice")

    assert amendment.status == "proposed"
    assert amendment.reviewer == "alice"
    assert amendment.published_at is None
    assert amendment.document == "document.md"
    assert amendment.adjudication.stage_id == "map_status"
    # The prose captures BOTH what was ambiguous and what was decided.
    assert "Map bill status" in amendment.prose
    assert "withdrawn bill's status was left unpinned" in amendment.prose
    assert "maps to null" in amendment.prose
    assert "alice" in amendment.prose


def test_record_persists_and_round_trips(tmp_path: Path) -> None:
    _write_doc(tmp_path)
    created = methodology.record_adjudication(tmp_path, _adj())

    loaded = methodology.load_amendment(tmp_path, created.id)
    assert loaded == created

    listed = methodology.list_amendments(tmp_path)
    assert [a.id for a in listed] == [created.id]


def test_edited_test_resolution_names_the_test(tmp_path: Path) -> None:
    _write_doc(tmp_path)
    amendment = methodology.record_adjudication(
        tmp_path,
        _adj(resolution_kind="edited_test", test_name="withdrawn_bill_maps_to_null"),
    )
    assert "withdrawn_bill_maps_to_null" in amendment.prose
    assert "guarded by the test" in amendment.prose


def test_record_without_document_still_captures_proposal(tmp_path: Path) -> None:
    # No document on disk yet: the proposal is still captured (truthful absence),
    # document is None, and it is NOT auto-published.
    amendment = methodology.record_adjudication(tmp_path, _adj())
    assert amendment.status == "proposed"
    assert amendment.document is None


def test_publish_appends_prose_to_canonical_document(tmp_path: Path) -> None:
    doc = _write_doc(tmp_path)
    original = doc.read_text(encoding="utf-8")
    amendment = methodology.record_adjudication(tmp_path, _adj())

    published = methodology.publish_amendment(tmp_path, amendment.id)

    assert published.status == "published"
    assert published.published_at is not None

    doc_text = doc.read_text(encoding="utf-8")
    # Original prose preserved; the resolution now lives IN the canonical prose.
    assert doc_text.startswith(original)
    assert "maps to null" in doc_text
    assert f"methodology-amendment:{amendment.id}" in doc_text
    # The persisted record reflects the published status too.
    assert methodology.load_amendment(tmp_path, amendment.id).status == "published"


def test_publish_without_document_fails_loudly(tmp_path: Path) -> None:
    amendment = methodology.record_adjudication(tmp_path, _adj())
    with pytest.raises(FileNotFoundError):
        methodology.publish_amendment(tmp_path, amendment.id)


def test_double_publish_raises_and_does_not_duplicate(tmp_path: Path) -> None:
    doc = _write_doc(tmp_path)
    amendment = methodology.record_adjudication(tmp_path, _adj())
    methodology.publish_amendment(tmp_path, amendment.id)

    with pytest.raises(ValueError):
        methodology.publish_amendment(tmp_path, amendment.id)

    # Exactly one amendment block in the document.
    assert doc.read_text(encoding="utf-8").count(
        f"methodology-amendment:{amendment.id}"
    ) == 1


def test_reject_leaves_document_untouched(tmp_path: Path) -> None:
    doc = _write_doc(tmp_path)
    original = doc.read_text(encoding="utf-8")
    amendment = methodology.record_adjudication(tmp_path, _adj())

    rejected = methodology.reject_amendment(tmp_path, amendment.id, note="wrong stage")

    assert rejected.status == "rejected"
    assert doc.read_text(encoding="utf-8") == original
    assert "wrong stage" in rejected.prose


def test_cannot_publish_a_rejected_amendment(tmp_path: Path) -> None:
    _write_doc(tmp_path)
    amendment = methodology.record_adjudication(tmp_path, _adj())
    methodology.reject_amendment(tmp_path, amendment.id)
    with pytest.raises(ValueError):
        methodology.publish_amendment(tmp_path, amendment.id)


def test_cannot_reject_a_published_amendment(tmp_path: Path) -> None:
    _write_doc(tmp_path)
    amendment = methodology.record_adjudication(tmp_path, _adj())
    methodology.publish_amendment(tmp_path, amendment.id)
    with pytest.raises(ValueError):
        methodology.reject_amendment(tmp_path, amendment.id)


def test_publish_targets_legacy_methodology_raw(tmp_path: Path) -> None:
    # A project whose canonical document is methodology_raw.md (no document.md) is
    # amended in that file — the amendment flow uses the same document probe as the
    # rest of the app, so it follows the project's real prose file.
    tmp_path.mkdir(parents=True, exist_ok=True)
    doc = tmp_path / "methodology_raw.md"
    doc.write_text("# Legacy prose\n", encoding="utf-8")

    amendment = methodology.record_adjudication(tmp_path, _adj())
    assert amendment.document == "methodology_raw.md"
    methodology.publish_amendment(tmp_path, amendment.id)
    assert "maps to null" in doc.read_text(encoding="utf-8")


def test_list_is_newest_first(tmp_path: Path) -> None:
    _write_doc(tmp_path)
    a1 = methodology.record_adjudication(tmp_path, _adj(stage_id="s1"))
    # Force a distinct, later id rather than depending on wall-clock spacing.
    a2 = a1.model_copy(update={"id": "20990101T000000"})
    methodology._write(tmp_path, a2)

    listed = methodology.list_amendments(tmp_path)
    assert listed[0].id == "20990101T000000"
