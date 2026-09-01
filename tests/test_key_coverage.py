"""Key coverage over the case that motivated it: BuzzFeed's 2015 refugee arrivals by
state (#469 case 1). Wyoming accepted none, so aggregating by state gives it no row and
the enrich that follows drops a jurisdiction while every other instrument reads clean.
The frames carry key columns only — no arrival or population figure is needed to say
which keys are absent."""
from __future__ import annotations


import json

import pandas as pd

from app.models import parse_stage
from app.models.severity import UserFacingErrorSeverity
from app.runtime.key_coverage import find_key_coverage_issues
from app.runtime.runner import execute_run
from app.services.project import save_working_copy_as_version
from app.web.run_issues import build_run_issues
from conftest import as_inputs, pinned_stages, place_stage
from stage_seed import add_stage

# The 50 states and DC — the 51 jurisdictions the published table ranked.
STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]
JURISDICTIONS = pd.DataFrame({
    "state": [*STATES, "DC"],
    "jurisdiction_type": ["state"] * len(STATES) + ["federal district"],
})
# What aggregating arrivals by state produced: Wyoming took no refugees over the
# window so it has no row, and the two territories the reference does not list do.
ARRIVALS_BY_STATE = pd.DataFrame({
    "state": [s for s in STATES if s != "WY"] + ["DC", "GU", "PR"],
})


def _column(name):
    return {"name": name, "type": "str", "nullable": False}


def _enrich(keys, enrich_with=None):
    return place_stage(parse_stage({
        "id": "arrivals_with_jurisdiction",
        "description": "Add each jurisdiction's kind to the arrivals aggregate",
        "type": "enrich",
        "inputs": [{"id": "arrivals"}, {"id": "jurisdictions"}],
        "signature": {
            "form": "extends",
            "reads": [
                {"input": "arrivals",
                 "columns": [_column(k["left"]) for k in keys]},
                {"input": "jurisdictions",
                 "columns": [_column(k["right"]) for k in keys]
                            + [_column("jurisdiction_type")]},
            ],
            "adds": [{"name": "jurisdiction_type", "type": "str", "nullable": True}],
        },
        "join": {
            "keys": keys,
            "enrich_with": enrich_with or {"jurisdiction_type": "jurisdiction_type"},
        },
    }))


_STATE_KEY = [{"left": "state", "right": "state"}]


def _issues(subject, reference, keys=None):
    return find_key_coverage_issues(
        _enrich(keys or _STATE_KEY),
        as_inputs({"arrivals": subject, "jurisdictions": reference}),
    )


def test_the_reference_key_that_reached_no_output_row_is_named():
    # This is the whole of case 1's disagreement with the published table.
    issues = _issues(ARRIVALS_BY_STATE, JURISDICTIONS)
    reference_side = [i for i in issues if i.message.startswith("The output holds no row")]
    assert len(reference_side) == 1
    assert "1 of the 51 key values" in reference_side[0].message
    assert "'WY'" in reference_side[0].message
    assert reference_side[0].column == "state"


def test_a_subject_key_the_reference_never_listed_is_named():
    # The Guam half nobody noticed in 2015 either.
    issues = _issues(ARRIVALS_BY_STATE, JURISDICTIONS)
    subject_side = [i for i in issues if i.message.startswith("Reference input")]
    assert len(subject_side) == 1
    assert "2 of the 52 key values" in subject_side[0].message
    assert "'GU'" in subject_side[0].message and "'PR'" in subject_side[0].message


def test_a_coverage_gap_is_a_warning_and_never_stops_the_run():
    assert {i.severity for i in _issues(ARRIVALS_BY_STATE, JURISDICTIONS)} == {
        UserFacingErrorSeverity.warning
    }


def test_matching_key_sets_raise_nothing():
    assert _issues(JURISDICTIONS[["state"]], JURISDICTIONS) == []


def test_a_null_key_is_not_reported_as_an_absent_value():
    # Reporting it here would name `nan` as a jurisdiction the reference omits.
    subject = pd.DataFrame({"state": [*JURISDICTIONS["state"], None]})
    assert _issues(subject, JURISDICTIONS) == []


def test_an_empty_subject_names_every_reference_key_it_can_and_counts_the_rest():
    issues = _issues(pd.DataFrame({"state": []}), JURISDICTIONS)
    assert len(issues) == 1
    message = issues[0].message
    assert "51 of the 51 key values" in message
    # Ten named, the remaining 41 counted — a 51-value list is not a message.
    assert all(f"'{code}'" in message for code in
               ["AK", "AL", "AR", "AZ", "CA", "CO", "CT", "DC", "DE", "FL"])
    assert "'GA'" not in message and "'WY'" not in message
    assert "and 41 more" in message


def test_a_composite_key_is_named_as_the_pair_it_is():
    keys = [{"left": "state", "right": "state"}, {"left": "year", "right": "year"}]
    reference = pd.DataFrame({
        "state": ["CA", "CA"], "year": ["2014", "2015"],
        "jurisdiction_type": ["state", "state"],
    })
    issues = _issues(pd.DataFrame({"state": ["CA"], "year": ["2015"]}), reference, keys)
    assert len(issues) == 1
    assert "('CA', '2014')" in issues[0].message
    assert issues[0].column == "state+year"


def test_a_key_that_cannot_form_a_set_says_the_check_did_not_run():
    # Silence here would read as "checked, and covered".
    subject = pd.DataFrame({"state": [["CA", "OR"]]})
    issues = _issues(subject, JURISDICTIONS)
    assert len(issues) == 1
    assert "was not checked" in issues[0].message
    assert issues[0].severity == UserFacingErrorSeverity.warning


# ── the whole way out to the surface a reviewer reads ────────────────────────
def _write_wyoming_project(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    ARRIVALS_BY_STATE.to_csv(root / "data" / "arrivals.csv", index=False)
    JURISDICTIONS.to_csv(root / "data" / "jurisdictions.csv", index=False)
    for order, (sid, name, columns) in enumerate([
        ("arrivals", "arrivals", [_column("state")]),
        ("jurisdictions", "jurisdictions",
         [_column("state"), _column("jurisdiction_type")]),
    ], start=1):
        add_stage(root, {
            "id": sid, "description": f"Load {name}", "type": "input_data",
            "connector": {"kind": "file", "params": {
                "path": str(root / "data" / f"{name}.csv"), "format": "csv"}},
            "signature": {"form": "replaces", "produces": columns},
        })
    add_stage(root, json.loads(_enrich(_STATE_KEY).stage.model_dump_json(exclude_none=True)))


def test_the_gap_reaches_the_run_issue_index(tmp_path):
    # The unit tests above prove the check; this proves a reviewer is shown it.
    _write_wyoming_project(tmp_path)
    save_working_copy_as_version(tmp_path.name, message="wyoming")
    workflow, version = pinned_stages(tmp_path)
    manifest = execute_run(tmp_path / "runs", tmp_path.name, workflow, version)

    assert manifest["status"] == "ok"
    issues = build_run_issues(manifest, workflow.stages)
    [flagged] = [s for s in issues.flagged if s.stage_id == "arrivals_with_jurisdiction"]
    messages = [i.message for i in flagged.issues]
    assert any("'WY'" in m and m.startswith("The output holds no row") for m in messages)
    assert any("'GU'" in m and "'PR'" in m for m in messages)
    # A coverage gap is advisory: it is counted as a warning and the run still ran.
    assert {i.severity for i in flagged.issues} == {UserFacingErrorSeverity.warning}
    assert issues.error_count == 0
