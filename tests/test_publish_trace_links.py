"""Published output links each row to its provenance trace."""
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.loading as loading
from app.core.errors import TraceLinksUnavailableError
from app.main import app
from app.models import parse_stage, Stage
from app.runtime.context import RunContext
from app.runtime.stages.publish import handle_publish
from app.runtime.trace_links import RowTraceLinker
from test_trace_helpers import write_run


# ── the link shape ────────────────────────────────────────────────────────────

def test_build_row_trace_url_matches_the_trace_view_route():
    linker = RowTraceLinker(project="palm", run_id="20260723T101500")
    assert linker.build_row_trace_url("score_rows", 4) == (
        "/project/palm/runs/20260723T101500/stage/score_rows/row/4/trace/view"
    )


def test_build_row_trace_url_percent_encodes_each_segment():
    linker = RowTraceLinker(project="my project", run_id="a/b")
    url = linker.build_row_trace_url("stage one", 0)
    assert url == "/project/my%20project/runs/a%2Fb/stage/stage%20one/row/0/trace/view"


def test_build_row_trace_url_rejects_a_negative_ordinal():
    linker = RowTraceLinker(project="palm", run_id="R1")
    with pytest.raises(ValueError):
        linker.build_row_trace_url("score_rows", -1)


# ── what the publish handler passes ───────────────────────────────────────────

_LINKING_PUBLISH_CODE = """
import pathlib

def transform(df, output_dir, trace_links):
    rows = [
        "<li><a href='" + trace_links.build_row_trace_url("enrich", i) + "'>"
        + str(row["name"]) + "</a></li>"
        for i, row in enumerate(df.to_dict("records"))
    ]
    path = pathlib.Path(output_dir) / "index.html"
    path.write_text("<ul>" + "".join(rows) + "</ul>", encoding="utf-8")
    return pd.DataFrame({"path": [str(path)]})
"""

_PLAIN_PUBLISH_CODE = """
import pathlib

def transform(df, output_dir):
    path = pathlib.Path(output_dir) / "index.html"
    path.write_text("<p>no links</p>", encoding="utf-8")
    return pd.DataFrame({"path": [str(path)]})
"""


_NAME_COLUMN = [{"name": "name", "type": "str"}]


def _publish_stage(code: str, input_columns=_NAME_COLUMN) -> Stage:
    return parse_stage({
        "id": "report",
        "type": "publish",
        "name": "Report",
        "inputs": [{"id": "enrich", "schema": {"columns": input_columns}}],
        "publish": {"format": "html_report", "destination": "build/"},
        "function": {"kind": "inline", "code": "import pandas as pd\n" + code},
    })


_FRAME = pd.DataFrame({"name": ["Alpha", "Beta"]})


def test_handler_passes_a_linker_when_the_function_declares_it(tmp_path):
    ctx = RunContext.for_workflow_run(
        repo_root=tmp_path, run_dir=tmp_path / "run", project="palm", run_id="R1",
    )
    result = handle_publish(_publish_stage(_LINKING_PUBLISH_CODE), {"enrich": _FRAME}, ctx)
    html = (tmp_path / "run" / "artifacts" / "build" / "index.html").read_text(encoding="utf-8")
    assert "/project/palm/runs/R1/stage/enrich/row/0/trace/view" in html
    assert "/project/palm/runs/R1/stage/enrich/row/1/trace/view" in html
    assert len(result) == 1


def test_handler_leaves_a_function_without_the_keyword_untouched(tmp_path):
    ctx = RunContext.for_workflow_run(
        repo_root=tmp_path, run_dir=tmp_path / "run", project="palm", run_id="R1",
    )
    handle_publish(_publish_stage(_PLAIN_PUBLISH_CODE), {"enrich": _FRAME}, ctx)
    html = (tmp_path / "run" / "artifacts" / "build" / "index.html").read_text(encoding="utf-8")
    assert html == "<p>no links</p>"


def test_handler_fails_loudly_when_a_scopeless_run_cannot_address_a_trace(tmp_path):
    ctx = RunContext.for_stages_outside_a_run(repo_root=tmp_path, run_dir=tmp_path / "run")
    with pytest.raises(TraceLinksUnavailableError) as exc:
        handle_publish(_publish_stage(_LINKING_PUBLISH_CODE), {"enrich": _FRAME}, ctx)
    assert "report" in str(exc.value)


# ── the emitted link resolves against the live app ────────────────────────────

def test_a_link_emitted_into_published_html_resolves(tmp_path, monkeypatch):
    """The publish function writes hrefs with the linker; the trace routes serve
    them. Both halves run for real — only the run directory is a fixture."""
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    seeds = pd.DataFrame({"facility_id": ["a", "b"], "name": ["Alpha", "Beta"]})
    enrich = seeds.assign(score=[1, 2])
    run_dir = write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ], run_id="R1")

    ctx = RunContext.for_workflow_run(
        repo_root=tmp_path, run_dir=run_dir, project="proj", run_id="R1",
    )
    enrich_columns = [{"name": "facility_id", "type": "str"}, *_NAME_COLUMN,
                      {"name": "score", "type": "int"}]
    handle_publish(
        _publish_stage(_LINKING_PUBLISH_CODE, input_columns=enrich_columns),
        {"enrich": enrich}, ctx)
    html = (run_dir / "artifacts" / "build" / "index.html").read_text(encoding="utf-8")

    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    client = TestClient(app)
    for href in _hrefs(html):
        assert client.get(href).status_code == 200
        assert client.get(href.removesuffix("/view")).status_code == 200

    # and the trace those links address really walks back to the source rows
    traced = json.loads(client.get(_hrefs(html)[0].removesuffix("/view")).text)
    assert [step["stage_id"] for step in traced["steps"]] == ["enrich", "seeds"]
    assert traced["steps"][0]["row"]["name"] == "Alpha"


def _hrefs(html: str) -> list[str]:
    return [chunk.split("'", 1)[0] for chunk in html.split("href='")[1:]]
