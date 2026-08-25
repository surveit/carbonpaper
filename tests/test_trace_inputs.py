"""The Inputs pane: the files the run read, listed file first."""
from __future__ import annotations

import pandas as pd

from app.core import files as file_store
from app.models import Stage, Workflow, parse_stage
from app.web.panel_links import AppPanelLinks, PacketPanelLinks
from app.web.trace_inputs import build_input_catalog, read_run_inputs

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



def _inputs(manifest, links=None):
    return read_run_inputs(build_input_catalog(PROJECT, manifest),
                           links or AppPanelLinks(PROJECT, "T1"))


def _file(inputs, filename: str):
    return next(f for f in inputs.files if f.filename == filename)


def test_every_file_the_run_read_is_listed(tmp_path):
    inputs = _inputs(_manifest(["filings", "contracts"]))

    # The run's list, not the row's: the reference side is here on the same footing.
    assert [f.filename for f in inputs.files] == ["east.csv", "contracts.csv"]


def test_the_pane_is_the_same_for_every_row_of_the_run(tmp_path):
    """It reads the manifest and nothing else, so no walk can narrow or widen it."""
    manifest = _manifest(["filings", "contracts"])

    assert _inputs(manifest) == _inputs(manifest)


def test_a_file_names_the_stage_that_read_it(tmp_path):
    read = _file(_inputs(_manifest(["filings"])), "east.csv")

    assert (read.path, read.read_by) == ("/data/east.csv", "filings")
    assert read.read_by_href is not None


def test_a_file_whose_bytes_the_project_holds_links_its_page(tmp_path):
    file_store.ProjectFile(sha256=EAST_SHA, filename="east.csv",
                           byte_count=EAST_BYTES, project_id=PROJECT).save()

    read = _file(_inputs(_manifest(["filings"])), "east.csv")
    stored = file_store.ProjectFile.find(sha256=EAST_SHA)[0]
    assert read.href == f"/project/{PROJECT}/files/{stored.id}"


def test_bytes_no_stored_file_holds_are_stated_as_unheld(tmp_path):
    assert _file(_inputs(_manifest(["filings"])), "east.csv").href is None


def test_a_packet_offers_no_file_page(tmp_path):
    """A folder has no route to serve one, so the entry stands without a link."""
    file_store.ProjectFile(sha256=EAST_SHA, filename="east.csv",
                           byte_count=EAST_BYTES, project_id=PROJECT).save()

    read = _file(_inputs(_manifest(["filings"]), links=PacketPanelLinks()), "east.csv")
    assert read.href is None


def test_the_row_cap_the_run_set_is_reported(tmp_path):
    inputs = _inputs(_manifest(["filings"], limits={"filings": 50}))

    assert _file(inputs, "east.csv").row_cap == 50


def test_what_the_stage_did_comes_off_its_own_record(tmp_path):
    read = _file(_inputs(_manifest(["filings"])), "east.csv")

    assert (read.status, read.rows_out) == ("ok", 2)


def test_an_input_stage_the_manifest_names_no_file_for_is_listed_apart(tmp_path):
    manifest = _manifest(["filings"])
    manifest["input_bindings"] = {}

    inputs = _inputs(manifest)
    assert inputs.files == []
    assert [s.stage_id for s in inputs.unnamed] == ["filings"]


def test_a_stage_the_manifest_never_recorded_claims_nothing(tmp_path):
    manifest = _manifest(["filings"])
    manifest["stage_records"] = []

    inputs = _inputs(manifest)
    # No record, so no stage to list — the pane says the run records no input stage.
    assert (inputs.files, inputs.unnamed) == ([], [])
