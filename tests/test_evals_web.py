"""Web tests for the read-only eval pages: home (evals_index), config
(eval_detail), and run (eval_run_detail). All against a tmp methodology dir —
nothing here touches examples/ or the repo's real methodologies."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import app.web.loading as loading
import app.web.routers.evals as evals_router
import app.web.routers.methodology as methodology_router
from app.main import app

client = TestClient(app)

METHODOLOGY = "tm"


def _stage_yaml(**over):
    base = {
        "id": "input_data", "type": "input_data", "name": "Input data",
        "connector": {"kind": "file", "params": {"path": "input.csv"}},
        "output_schema": {"columns": [{"name": "text", "type": "str"},
                                       {"name": "doc_id", "type": "str"}]},
    }
    base.update(over)
    return base


def _write_methodology(examples_root: Path, name: str) -> Path:
    """A minimal 3-stage workflow: input_data -> llm_transform -> publish."""
    meth_dir = examples_root / name
    compiled = meth_dir / "compiled"
    compiled.mkdir(parents=True)

    input_stage = _stage_yaml()
    llm_stage = {
        "id": "llm_transform", "type": "llm_transform", "name": "LLM transform",
        "inputs": ["input_data"],
        "llm": {"prompt_template": "Summarize: {text}"},
        "output_schema": {"columns": [{"name": "doc_id", "type": "str"},
                                       {"name": "summary", "type": "str"}]},
    }
    publish_stage = {
        "id": "publish", "type": "publish", "name": "Publish",
        "inputs": ["llm_transform"],
        "function": {"kind": "inline", "code": "publish(df)"},
        "publish": {"format": "json", "destination": "out.json"},
    }
    (compiled / "01_input_data.yaml").write_text(
        yaml.safe_dump(input_stage), encoding="utf-8")
    (compiled / "02_llm_transform.yaml").write_text(
        yaml.safe_dump(llm_stage), encoding="utf-8")
    (compiled / "03_publish.yaml").write_text(
        yaml.safe_dump(publish_stage), encoding="utf-8")
    return meth_dir


def _cases_csv(meth_dir: Path) -> Path:
    data_dir = meth_dir / "eval_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "cases.csv"
    path.write_text("doc_id,text,expected_summary\nd1,hello world,hi\n", encoding="utf-8")
    return path


def _valid_config_yaml(meth_dir: Path) -> None:
    _cases_csv(meth_dir)
    config = {
        "id": "valid-eval",
        "methodology": METHODOLOGY,
        "name": "Valid eval",
        "override_stage": "input_data",
        "target_stage": "llm_transform",
        "table": {
            # repo-root-relative, matching the input_data-stage convention --
            # meth_dir is examples/tm under the fixture's tmp repo root.
            "path": f"examples/{METHODOLOGY}/eval_data/cases.csv",
            "format": "csv",
            "table_schema": {"columns": [
                {"name": "doc_id", "type": "str"},
                {"name": "text", "type": "str"},
                {"name": "expected_summary", "type": "str"},
            ]},
        },
        "key": ["doc_id"],
        "input_columns": ["text"],
        "expected": [{"actual": "summary", "expected": "expected_summary"}],
    }
    config_dir = meth_dir / "eval_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "valid-eval.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8")


def _config_with_reference_override_yaml(meth_dir: Path) -> None:
    """A second valid config, distinct from valid-eval, that also sets a
    reference_overrides entry -- covers the pathway summary's separate
    "reference overrides" label (as opposed to override_stage's "overridden")."""
    config = {
        "id": "ref-override-eval",
        "methodology": METHODOLOGY,
        "name": "Ref override eval",
        "override_stage": "input_data",
        "target_stage": "llm_transform",
        "reference_overrides": [{
            "stage_id": "publish",
            "table": {
                "path": f"examples/{METHODOLOGY}/eval_data/cases.csv",
                "format": "csv",
                "table_schema": {"columns": [{"name": "doc_id", "type": "str"}]},
            },
        }],
        "table": {
            "path": f"examples/{METHODOLOGY}/eval_data/cases.csv",
            "format": "csv",
            "table_schema": {"columns": [
                {"name": "doc_id", "type": "str"},
                {"name": "text", "type": "str"},
                {"name": "expected_summary", "type": "str"},
            ]},
        },
        "key": ["doc_id"],
        "input_columns": ["text"],
        "expected": [{"actual": "summary", "expected": "expected_summary"}],
    }
    config_dir = meth_dir / "eval_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "ref-override-eval.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8")


def _broken_config_yaml(meth_dir: Path) -> None:
    config = {
        "id": "broken-eval",
        "methodology": METHODOLOGY,
        "name": "Broken eval",
        "override_stage": "input_data",
        "target_stage": "nonexistent_stage",
        "table": {
            "path": f"examples/{METHODOLOGY}/eval_data/cases.csv",
            "format": "csv",
            "table_schema": {"columns": [{"name": "doc_id", "type": "str"}]},
        },
        "key": ["doc_id"],
        "input_columns": ["doc_id"],
        "expected": [{"actual": "x", "expected": "doc_id"}],
    }
    config_dir = meth_dir / "eval_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "broken-eval.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8")


def _write_run(meth_dir: Path, run_id: str, **over) -> None:
    run_dir = meth_dir / "eval_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    run = {
        "id": run_id,
        "config": "valid-eval",
        "methodology": METHODOLOGY,
        "methodology_version": "v1",
        "status": "scored",
        "settings": {"can_score_declaratively": True,
                     "frontier": ["llm_transform"], "blocking_stages": []},
        "metrics": {"exact_match_rate": 1.0},
        "started_at": "2026-01-01T00:00:00",
    }
    run.update(over)
    (run_dir / f"{run_id}.json").write_text(
        __import__("json").dumps(run), encoding="utf-8")


@pytest.fixture
def tmp_examples(tmp_path, monkeypatch):
    examples_root = tmp_path / "examples"
    examples_root.mkdir()
    meth_dir = _write_methodology(examples_root, METHODOLOGY)
    _valid_config_yaml(meth_dir)
    _broken_config_yaml(meth_dir)
    _config_with_reference_override_yaml(meth_dir)

    monkeypatch.setattr(loading, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(evals_router, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(methodology_router, "EXAMPLES_DIR", examples_root)
    # config.table.path is repo-root-relative (same convention input_data
    # stages use: repo_root / params["path"]) -- point REPO_ROOT at tmp_path
    # so the fixture's "examples/tm/eval_data/cases.csv" resolves for real.
    monkeypatch.setattr(evals_router, "REPO_ROOT", tmp_path)
    return meth_dir


# ── evals_index ───────────────────────────────────────────────────────────────
def test_evals_index_lists_both_evals_with_status(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals")
    assert r.status_code == 200
    assert "Valid eval" in r.text
    assert "Broken eval" in r.text
    assert "broken" in r.text
    assert "never run" in r.text


# ── eval_detail (config page) ────────────────────────────────────────────────
def test_eval_detail_valid_config_page(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/valid-eval")
    assert r.status_code == 200
    assert "input_data" in r.text
    assert "llm_transform" in r.text
    # cases-table columns rendered
    assert "expected_summary" in r.text
    assert "doc_id" in r.text
    # cases-table ROWS actually rendered: a distinctive cell value from the
    # fixture CSV that appears nowhere else on the page, proving the table
    # file was found and read (not just that its schema columns were echoed).
    assert "hello world" in r.text
    assert "load-issues" not in r.text


def test_eval_detail_pathway_splits_overridden_and_reference_overrides(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/ref-override-eval")
    assert r.status_code == 200
    assert "overridden:" in r.text
    assert "reference overrides:" in r.text
    # the reference-override stage id appears under its own label, not
    # folded into the "overridden" span alongside override_stage.
    assert "<code>publish</code>" in r.text


def test_eval_detail_no_reference_overrides_omits_that_label(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/valid-eval")
    assert r.status_code == 200
    assert "reference overrides:" not in r.text


def test_eval_detail_broken_config_shows_problems(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/broken-eval")
    assert r.status_code == 200
    assert "nonexistent_stage" in r.text
    assert "does not exist in the methodology" in r.text


def test_eval_detail_unknown_id_404(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/nope")
    assert r.status_code == 404


def test_eval_detail_dataless_shows_attach(tmp_examples):
    config = {
        "id": "dataless-eval",
        "methodology": METHODOLOGY,
        "name": "Dataless eval",
        "override_stage": "input_data",
        "target_stage": "llm_transform",
        "key": ["doc_id"],
        "input_columns": ["text"],
        "expected": [{"actual": "summary", "expected": "expected_summary"}],
    }
    config_dir = tmp_examples / "eval_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "dataless-eval.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8")

    r = client.get(f"/methodology/{METHODOLOGY}/evals/dataless-eval")
    assert r.status_code == 200
    assert "attach cases" in r.text.lower()
    assert "<th>doc_id</th>" not in r.text
    assert "no cases yet" in r.text


# ── eval_run_detail (run page) ───────────────────────────────────────────────
def test_eval_run_detail_shows_status_and_metrics(tmp_examples):
    _write_run(tmp_examples, "run-1")
    r = client.get(f"/methodology/{METHODOLOGY}/evals/valid-eval/runs/run-1")
    assert r.status_code == 200
    assert "scored" in r.text
    assert "exact_match_rate" in r.text


def test_eval_run_detail_unknown_run_404(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/valid-eval/runs/nope")
    assert r.status_code == 404


def test_eval_run_detail_renders_when_sibling_run_is_corrupt(tmp_examples):
    """A requested run must render even when some OTHER run file for the same
    eval is corrupt -- the old behavior 404'd every run page in this
    situation because it listed all runs to find the one requested."""
    _write_run(tmp_examples, "run-1")
    (tmp_examples / "eval_run" / "run-bad.json").write_text(
        "not json at all {", encoding="utf-8")

    r = client.get(f"/methodology/{METHODOLOGY}/evals/valid-eval/runs/run-1")
    assert r.status_code == 200
    assert "scored" in r.text
    assert "exact_match_rate" in r.text


def test_eval_run_detail_malformed_run_is_422_not_404(tmp_examples):
    run_dir = tmp_examples / "eval_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "broken.json").write_text("not json at all {", encoding="utf-8")

    r = client.get(f"/methodology/{METHODOLOGY}/evals/valid-eval/runs/broken")
    assert r.status_code == 422


# ── methodology page nav link ────────────────────────────────────────────────
def test_methodology_page_has_evals_link(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}")
    assert r.status_code == 200
    assert ">Evals<" in r.text


# ── methodology page: eval pathway overlay ──────────────────────────────────
def test_methodology_page_has_overlay_data_with_eval_id(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}")
    assert r.status_code == 200
    assert 'id="eval-overlay-data"' in r.text
    assert "valid-eval" in r.text


def test_methodology_page_popover_shows_status(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}")
    assert r.status_code == 200
    assert "eval-popover" in r.text
    assert "never run" in r.text
    assert "broken" in r.text


def test_methodology_page_zero_evals_shows_no_evals_yet_and_no_warning(tmp_path, monkeypatch):
    examples_root = tmp_path / "examples"
    examples_root.mkdir()
    _write_methodology(examples_root, METHODOLOGY)
    monkeypatch.setattr(loading, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(evals_router, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(methodology_router, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", tmp_path)

    r = client.get(f"/methodology/{METHODOLOGY}")
    assert r.status_code == 200
    assert "No evals yet" in r.text
    assert "have no eval coverage" not in r.text


def test_methodology_page_uncovered_stage_names_it(tmp_path, monkeypatch):
    """publish is not on any eval's pathway when only valid-eval exists (it
    only covers input_data -> llm_transform), so the warning strip should name
    it."""
    examples_root = tmp_path / "examples"
    examples_root.mkdir()
    meth_dir = _write_methodology(examples_root, METHODOLOGY)
    _valid_config_yaml(meth_dir)
    monkeypatch.setattr(loading, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(evals_router, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(methodology_router, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", tmp_path)

    r = client.get(f"/methodology/{METHODOLOGY}")
    assert r.status_code == 200
    assert "have no eval coverage" in r.text
    assert "publish" in r.text


# ── stage partial: evals touching this stage ────────────────────────────────
def test_stage_partial_shows_overridden_eval(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/stage/input_data/partial")
    assert r.status_code == 200
    assert "Valid eval" in r.text
    assert "overridden" in r.text


def test_stage_partial_shows_target_eval(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/stage/llm_transform/partial")
    assert r.status_code == 200
    assert "Valid eval" in r.text
    assert "target" in r.text


def test_stage_partial_no_coverage_warning_line(tmp_path, monkeypatch):
    """publish has no coverage when only valid-eval exists (which only
    touches input_data/llm_transform) -- the stage panel should show the
    quiet one-line warning, not omit the section (evals DO exist for the
    methodology, just not for this stage)."""
    examples_root = tmp_path / "examples"
    examples_root.mkdir()
    meth_dir = _write_methodology(examples_root, METHODOLOGY)
    _valid_config_yaml(meth_dir)
    monkeypatch.setattr(loading, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(evals_router, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(methodology_router, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", tmp_path)

    r = client.get(f"/methodology/{METHODOLOGY}/stage/publish/partial")
    assert r.status_code == 200
    assert "No eval covers this stage." in r.text


def test_stage_partial_omits_eval_panel_when_zero_evals(tmp_path, monkeypatch):
    examples_root = tmp_path / "examples"
    examples_root.mkdir()
    _write_methodology(examples_root, METHODOLOGY)
    monkeypatch.setattr(loading, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(evals_router, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(methodology_router, "EXAMPLES_DIR", examples_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", tmp_path)

    r = client.get(f"/methodology/{METHODOLOGY}/stage/publish/partial")
    assert r.status_code == 200
    assert "No eval covers this stage." not in r.text
    assert "Evals touching this stage" not in r.text


# ── authoring form: GET new / edit ──────────────────────────────────────────
def test_get_new_lists_stages_and_disables_schemaless(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/new")
    assert r.status_code == 200
    assert "input_data" in r.text
    assert "llm_transform" in r.text
    assert "publish" in r.text
    # publish has no output_schema in the fixture -- its <option> must be
    # disabled, with a reason visible next to it.
    assert re.search(
        r'<option[^>]*value="publish"[^>]*disabled[^>]*>[^<]*\(no output schema\)',
        r.text,
    )


def test_get_edit_unknown_id_404(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/nope/edit")
    assert r.status_code == 404


def test_get_edit_prefills_existing_values(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/valid-eval/edit")
    assert r.status_code == 200
    assert "Valid eval" in r.text
    assert "doc_id" in r.text


# ── stage-schema JSON ───────────────────────────────────────────────────────
def test_stage_schema_json_for_schemaed_stage(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/stage-schema/input_data.json")
    assert r.status_code == 200
    body = r.json()
    assert body["stage_id"] == "input_data"
    names = {c["name"] for c in body["columns"]}
    assert names == {"text", "doc_id"}


def test_stage_schema_json_422_for_schemaless_stage(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/stage-schema/publish.json")
    assert r.status_code == 422
    assert "declares no output schema" in r.json()["error"]


def test_stage_schema_json_404_for_unknown_stage(tmp_examples):
    r = client.get(f"/methodology/{METHODOLOGY}/evals/stage-schema/nope.json")
    assert r.status_code == 404


# ── inspect-table ────────────────────────────────────────────────────────────
def test_inspect_table_upload_then_collision(tmp_examples):
    csv_bytes = b"doc_id,text,expected_summary\nd9,hi there,hiya\n"
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/inspect-table",
        files={"file": ("new_cases.csv", csv_bytes, "text/csv")},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["columns"]) == {"doc_id", "text", "expected_summary"}
    assert body["path"] == f"examples/{METHODOLOGY}/eval_data/new_cases.csv"
    assert (tmp_examples / "eval_data" / "new_cases.csv").is_file()

    r2 = client.post(
        f"/methodology/{METHODOLOGY}/evals/inspect-table",
        files={"file": ("new_cases.csv", csv_bytes, "text/csv")},
    )
    assert r2.status_code == 409


def test_inspect_table_by_path(tmp_examples):
    # cases.csv already exists under eval_data/ from the fixture.
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/inspect-table",
        data={"path": f"examples/{METHODOLOGY}/eval_data/cases.csv"},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["columns"]) == {"doc_id", "text", "expected_summary"}
    assert body["path"] == f"examples/{METHODOLOGY}/eval_data/cases.csv"


def test_inspect_table_by_path_404_when_missing(tmp_examples):
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/inspect-table",
        data={"path": f"examples/{METHODOLOGY}/eval_data/does_not_exist.csv"},
    )
    assert r.status_code == 404


# ── POST create / edit ──────────────────────────────────────────────────────
def _valid_create_payload(table_path: str) -> dict:
    return {
        "id": "new-eval",
        "name": "New eval",
        "description": "",
        "override_stage": "input_data",
        "target_stage": "llm_transform",
        "table_path": table_path,
        "table_format": "csv",
        "key": ["doc_id"],
        "input_columns": ["text"],
        "expected_actual": ["summary"],
        "expected_dataset": ["expected_summary"],
        "expected_metric": ["exact"],
        "expected_tolerance": [""],
    }


def test_post_create_valid_redirects_and_saves(tmp_examples):
    payload = _valid_create_payload(f"examples/{METHODOLOGY}/eval_data/cases.csv")
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/new",
        data=payload,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/methodology/{METHODOLOGY}/evals/new-eval"

    saved = tmp_examples / "eval_config" / "new-eval.yaml"
    assert saved.is_file()

    home = client.get(f"/methodology/{METHODOLOGY}/evals")
    assert home.status_code == 200
    assert "New eval" in home.text
    assert "never run" in home.text


def test_post_create_invalid_actual_column_rerenders_without_saving(tmp_examples):
    payload = _valid_create_payload(f"examples/{METHODOLOGY}/eval_data/cases.csv")
    payload["id"] = "bad-eval"
    payload["expected_actual"] = ["not_a_real_column"]
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/new",
        data=payload,
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "not_a_real_column" in r.text

    saved = tmp_examples / "eval_config" / "bad-eval.yaml"
    assert not saved.is_file()


def test_post_create_non_numeric_tolerance_rerenders_without_saving(tmp_examples):
    payload = _valid_create_payload(f"examples/{METHODOLOGY}/eval_data/cases.csv")
    payload["id"] = "bad-tolerance-eval"
    payload["expected_tolerance"] = ["not_a_number"]
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/new",
        data=payload,
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "is not a number" in r.text

    saved = tmp_examples / "eval_config" / "bad-tolerance-eval.yaml"
    assert not saved.is_file()


def test_post_create_existing_id_rejected_without_overwriting(tmp_examples):
    config_path = tmp_examples / "eval_config" / "valid-eval.yaml"
    original_bytes = config_path.read_bytes()

    payload = _valid_create_payload(f"examples/{METHODOLOGY}/eval_data/cases.csv")
    payload["id"] = "valid-eval"  # already exists from the fixture
    payload["name"] = "Attempted clobber"
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/new",
        data=payload,
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "already exists" in r.text

    assert config_path.read_bytes() == original_bytes


def test_post_edit_changes_description_only(tmp_examples):
    payload = _valid_create_payload(f"examples/{METHODOLOGY}/eval_data/cases.csv")
    payload["description"] = "updated description"
    del payload["id"]  # edit posts back to the path eval_id; id can't change
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/valid-eval/edit",
        data=payload,
        follow_redirects=False,
    )
    assert r.status_code == 303

    from app.services.eval_store import load_eval_config
    reloaded = load_eval_config(tmp_examples, "valid-eval")
    assert reloaded.id == "valid-eval"
    assert reloaded.description == "updated description"
    assert reloaded.override_stage == "input_data"
    assert reloaded.target_stage == "llm_transform"


# ── attach-cases (dataless eval) ─────────────────────────────────────────────
def _dataless_config_yaml(meth_dir: Path, eval_id: str = "dataless-eval") -> None:
    config = {
        "id": eval_id,
        "methodology": METHODOLOGY,
        "name": "Dataless eval",
        "override_stage": "input_data",
        "target_stage": "llm_transform",
        "key": ["doc_id"],
        "input_columns": ["text"],
        "expected": [{"actual": "summary", "expected": "expected_summary"}],
    }
    config_dir = meth_dir / "eval_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"{eval_id}.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8")


def test_attach_cases_to_dataless_eval(tmp_examples):
    _dataless_config_yaml(tmp_examples)

    # Derived schema: doc_id + text (input_data's output schema, via key +
    # input_columns) and expected_summary (llm_transform's `summary` column,
    # via the expected row's dataset name).
    csv_bytes = b"doc_id,text,expected_summary\nd1,hello world,hi\n"
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/dataless-eval/attach-cases",
        files={"file": ("dataless_cases.csv", csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/methodology/{METHODOLOGY}/evals/dataless-eval"

    from app.services.eval_store import load_eval_config
    reloaded = load_eval_config(tmp_examples, "dataless-eval")
    assert reloaded.table is not None
    assert "eval_data/" in reloaded.table.path

    detail = client.get(f"/methodology/{METHODOLOGY}/evals/dataless-eval")
    assert detail.status_code == 200
    assert "<th>doc_id</th>" in detail.text
    assert "no cases yet" not in detail.text


def test_attach_cases_rejects_mismatched_file(tmp_examples):
    _dataless_config_yaml(tmp_examples, eval_id="dataless-eval-2")

    # Missing the required `expected_summary` column.
    csv_bytes = b"doc_id,text\nd1,hello world\n"
    r = client.post(
        f"/methodology/{METHODOLOGY}/evals/dataless-eval-2/attach-cases",
        files={"file": ("mismatched_cases.csv", csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "expected_summary" in r.text

    from app.services.eval_store import load_eval_config
    reloaded = load_eval_config(tmp_examples, "dataless-eval-2")
    assert reloaded.table is None
