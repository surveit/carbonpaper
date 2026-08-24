"""The Inputs pane: what the run read, and which of it this row came in at."""
from __future__ import annotations

import pandas as pd

from app.core import files as file_store
from app.models import Stage, Workflow, parse_stage
from app.runtime.lineage import EdgeKind, RowLineage, RowParent
from app.runtime.trace import trace_row, trace_to_dict
from app.web.panel_links import AppPanelLinks, PacketPanelLinks
from app.web.trace_inputs import build_input_catalog, select_row_inputs
from app.web.trace_view import build_trace_view
from test_trace_helpers import write_run

PROJECT = "proj"
FILINGS = pd.DataFrame({"client": ["Acme", "Borealis"], "amount": [500, 1200]})
CONTRACTS = pd.DataFrame({"client": ["Acme"], "agency": ["HHS"]})
JOINED = pd.DataFrame({"client": ["Acme", "Borealis"], "amount": [500, 1200],
                       "agency": ["HHS", None]})
EAST_SHA = "e" * 64
REF_SHA = "c" * 64
EAST_BYTES = 791


def _column(name: str, kind: str = "str", nullable: bool = False) -> dict:
    return {"name": name, "type": kind, "nullable": nullable}


CLIENT, AMOUNT, AGENCY = _column("client"), _column("amount", "int"), _column("agency")


def _input_stage(stage_id: str, columns: list[dict]) -> Stage:
    return parse_stage({
        "id": stage_id, "description": stage_id, "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"paths": [f"/data/{stage_id}.csv"], "format": "csv"}},
        "signature": {"form": "replaces", "produces": columns},
    })


def _join_workflow() -> dict:
    join = parse_stage({
        "id": "j", "description": "j", "type": "enrich",
        "inputs": [{"id": "filings"}, {"id": "contracts"}],
        "join": {"keys": [{"left": "client", "right": "client"}],
                 "enrich_with": {"agency": "agency"}},
        "signature": {"form": "extends",
                      "reads": [{"input": "filings", "columns": [CLIENT]},
                                {"input": "contracts", "columns": [CLIENT, AGENCY]}],
                      "adds": [_column("agency", "str", True)]},
    })
    return Workflow(stages=[_input_stage("filings", [CLIENT, AMOUNT]),
                            _input_stage("contracts", [CLIENT, AGENCY]),
                            join]).index_workflow_stages_by_id()


def _manifest(stage_ids: list[str], limits: dict[str, int] | None = None) -> dict:
    files = {
        "filings": {"files": [{"path": "/data/east.csv", "sha256": EAST_SHA,
                               "bytes": EAST_BYTES}], "source": "run"},
        "contracts": {"files": [{"path": "/data/contracts.csv", "sha256": REF_SHA,
                                 "bytes": 120}], "source": "workflow"},
    }
    return {
        "run_id": "T1",
        "parameters": {"limits": limits or {}, "offsets": {}},
        "input_bindings": {sid: files[sid] for sid in stage_ids if sid in files},
        "stage_records": [
            {"stage_id": sid, "type": "input_data", "status": "ok",
             "output_row_count": 2, "started_at": "2026-08-13T18:16:47"}
            for sid in stage_ids
        ],
    }


def _join_run(tmp_path):
    lineage = RowLineage([[RowParent("filings", 0), RowParent("contracts", 0)],
                          [RowParent("filings", 1)]])
    return write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "contracts", "type": "input_data", "parents": [], "df": CONTRACTS},
        {"id": "j", "type": "enrich", "parents": ["filings", "contracts"],
         "df": JOINED, "lineage": lineage},
    ])


def _inputs(run_dir, stage: str, row: int, stages_by_id, manifest, links=None):
    links = links or AppPanelLinks(PROJECT, "T1")
    view = build_trace_view(
        trace_to_dict(trace_row(run_dir, stage, row)), stages_by_id, links)
    return select_row_inputs(
        build_input_catalog(PROJECT, manifest), view, links)


def _by_id(inputs, stage_id: str):
    return next(s for s in inputs.stages if s.stage_id == stage_id)


def test_every_input_the_run_read_is_listed(tmp_path):
    inputs = _inputs(_join_run(tmp_path), "j", 0,
                     _join_workflow(), _manifest(["filings", "contracts"]))

    # The run's list, not the row's: the reference side is here on the same footing.
    assert [s.stage_id for s in inputs.stages] == ["filings", "contracts"]


def test_the_stage_this_rows_walk_reached_is_the_one_marked(tmp_path):
    inputs = _inputs(_join_run(tmp_path), "j", 0,
                     _join_workflow(), _manifest(["filings", "contracts"]))

    assert inputs.row_entered_at == "filings"
    assert _by_id(inputs, "filings").row_came_through is True
    assert _by_id(inputs, "contracts").row_came_through is False


def test_a_row_whose_walk_stopped_short_marks_no_stage(tmp_path):
    """It summarizes its inputs, so no single input stage is where it came in."""
    run_dir = _summed_run(tmp_path)
    inputs = _inputs(run_dir, "totals", 0, {}, _manifest(["filings"]))

    assert inputs.row_entered_at is None
    assert [s.row_came_through for s in inputs.stages] == [False]
    # The file is still named — it is the run's input either way.
    assert [f.filename for f in _by_id(inputs, "filings").files] == ["east.csv"]


def test_the_file_names_itself_and_the_path_the_run_read(tmp_path):
    read = _by_id(_inputs(_join_run(tmp_path), "j", 0, _join_workflow(),
                          _manifest(["filings"])), "filings").files

    assert [(f.filename, f.path) for f in read] == [("east.csv", "/data/east.csv")]
    # This run recorded no file per row, so nothing claims which row of it this is.
    assert read[0].source_row is None


def test_a_file_whose_bytes_the_project_holds_links_its_page(tmp_path):
    file_store.ProjectFile(sha256=EAST_SHA, filename="east.csv",
                           byte_count=EAST_BYTES, project_id=PROJECT).save()

    read = _by_id(_inputs(_join_run(tmp_path), "j", 0, _join_workflow(),
                          _manifest(["filings"])), "filings").files[0]
    stored = file_store.ProjectFile.find(sha256=EAST_SHA)[0]
    assert read.href == f"/project/{PROJECT}/files/{stored.id}"


def test_bytes_no_stored_file_holds_are_stated_as_unheld(tmp_path):
    read = _by_id(_inputs(_join_run(tmp_path), "j", 0, _join_workflow(),
                          _manifest(["filings"])), "filings").files[0]

    assert read.href is None


def test_a_packet_offers_no_file_page(tmp_path):
    """A folder has no route to serve one, so the entry stands without a link."""
    file_store.ProjectFile(sha256=EAST_SHA, filename="east.csv",
                           byte_count=EAST_BYTES, project_id=PROJECT).save()

    read = _by_id(_inputs(_join_run(tmp_path), "j", 0, _join_workflow(),
                          _manifest(["filings"]), links=PacketPanelLinks()),
                  "filings").files[0]
    assert read.href is None


def test_the_row_cap_the_run_set_is_reported(tmp_path):
    inputs = _inputs(_join_run(tmp_path), "j", 0, _join_workflow(),
                     _manifest(["filings"], limits={"filings": 50}))

    assert _by_id(inputs, "filings").row_cap == 50


def test_what_the_stage_did_comes_off_its_own_record(tmp_path):
    ran = _by_id(_inputs(_join_run(tmp_path), "j", 0, _join_workflow(),
                         _manifest(["filings"])), "filings")

    assert (ran.status, ran.rows_out) == ("ok", 2)
    assert ran.read_at == "2026-08-13T18:16:47"


def test_a_stage_the_manifest_never_recorded_claims_nothing(tmp_path):
    manifest = _manifest(["filings"])
    manifest["stage_records"] = []

    inputs = _inputs(_join_run(tmp_path), "j", 0, _join_workflow(), manifest)
    # No record, so no stage to list — the pane says the run records no input stage.
    assert inputs.stages == []


def _summed_run(tmp_path):
    lineage = RowLineage([[RowParent("filings", 0, EdgeKind.contribution.value),
                           RowParent("filings", 1, EdgeKind.contribution.value)]])
    return write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "totals", "type": "aggregate", "parents": ["filings"],
         "df": pd.DataFrame({"total": [1700]}), "lineage": lineage},
    ])
