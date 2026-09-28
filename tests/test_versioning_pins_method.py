"""A version keeps the project's row types, verbs and methodology; each reader reads that copy."""
from __future__ import annotations

import pandas as pd
from markupsafe import escape

from app.core.persistence import get_store
from app.models.records.workflow_version import Method, WorkflowVersion
from app.models.row_types import RowType
from app.models.terms import Terms, Verb
from app.services import run as run_service
from app.services import terms as terms_service
from app.services.methodology import write_methodology
from app.services.project import create_project
from app.services.versioning import create_version_from_stages, load_version
from app.services.workspace import resolve_project_dir
from app.web.review_packet import export_review_packet

_METHODOLOGY = "Count the filings each firm made."
_REWRITTEN = "Count the firms, then their filings."
_TERMS = Terms(
    row_types=[RowType(id="filing", title="Filing", definition="One disclosure a firm made.")],
    verbs=[Verb(name="flag", definition="Mark a row for a human to decide on.")],
)
_KEPT = Method(row_types=_TERMS.row_types, verbs=_TERMS.verbs, methodology=_METHODOLOGY)


# ── the writer ──


def test_a_version_keeps_what_the_project_held_when_it_was_saved(projects_root):
    project_id = _create_project()
    version = _save_version(project_id)
    _rewrite_the_project(project_id)

    assert load_version(project_id, version.version_id).method == _KEPT


def test_a_project_with_no_methodology_is_kept_as_having_none(tmp_path):
    version = create_version_from_stages(
        tmp_path.name, [_load_stage(tmp_path / "rows.csv")], message="v1")

    kept = load_version(tmp_path.name, version.version_id).method
    assert kept is not None and kept.methodology is None


def test_a_version_stored_before_versions_kept_a_method_loads_without_one(projects_root):
    project_id = _create_project()
    version = _save_version(project_id)
    _store_as_saved_before_versions_kept_a_method(version)

    assert load_version(project_id, version.version_id).method is None


# ── the review packet ──


def test_the_packet_writes_the_methodology_the_runs_version_kept(projects_root, tmp_path):
    project_id, run_id = _run_a_project()
    _rewrite_the_project(project_id)

    packet = export_review_packet(project_id, run_id, tmp_path / "packets")

    assert (packet.root / "methodology.md").read_text(encoding="utf-8") == _METHODOLOGY


def test_a_packet_on_an_older_version_says_why_it_holds_no_methodology(projects_root, tmp_path):
    project_id, run_id = _run_a_project()
    _store_as_saved_before_versions_kept_a_method(_load_the_runs_version(project_id, run_id))

    packet = export_review_packet(project_id, run_id, tmp_path / "packets")

    assert not (packet.root / "methodology.md").exists()
    [omitted] = [o for o in packet.omitted if o.path == "methodology.md"]
    assert "predates versions keeping the methodology" in omitted.reason
    index = (packet.root / "index.html").read_text(encoding="utf-8")
    assert str(escape(omitted.reason)) in index and _METHODOLOGY not in index


# ── helpers ──


def _create_project() -> str:
    project_id = create_project("pins", _METHODOLOGY, source="pinning test").id
    terms_service.write_terms(project_id, _TERMS)
    return project_id


def _rewrite_the_project(project_id: str) -> None:
    write_methodology(project_id, _REWRITTEN)
    terms_service.write_terms(project_id, Terms())


def _load_stage(path) -> dict:
    return {
        "id": "load", "description": "Load the filings", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "csv"}},
        "signature": {"form": "replaces",
                      "produces": [{"name": "firm", "type": "str", "nullable": False}]},
    }


def _save_version(project_id: str) -> WorkflowVersion:
    rows = resolve_project_dir(project_id) / "filings.csv"
    pd.DataFrame({"firm": ["Acme", "Birch"]}).to_csv(rows, index=False)
    return create_version_from_stages(project_id, [_load_stage(rows)], message="v1")


def _run_a_project() -> tuple[str, str]:
    project_id = _create_project()
    _save_version(project_id)
    return project_id, str(run_service.execute(project_id)["run_id"])


def _load_the_runs_version(project_id: str, run_id: str) -> WorkflowVersion:
    return load_version(project_id, run_service.read_pinned_version(project_id, run_id))


def _store_as_saved_before_versions_kept_a_method(version: WorkflowVersion) -> None:
    document = WorkflowVersion.load_raw(version.id)
    del document["method"]
    get_store().write(WorkflowVersion.collection, version.id, document,
                      schema_version=WorkflowVersion.SCHEMA_VERSION)
