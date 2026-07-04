"""Web tests for the read-only eval pages: home (evals_index), config
(eval_detail), and run (eval_run_detail). All against a tmp methodology dir —
nothing here touches examples/ or the repo's real methodologies."""
from __future__ import annotations

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
        "passed": True,
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
