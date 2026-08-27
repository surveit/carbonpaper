"""Published output links each row to its provenance trace."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from conftest import as_inputs, place_stage, rows_of
from fastapi.testclient import TestClient

from app.services import workspace
from app.core.errors import TraceLinksUnavailableError
from app.main import app
from app.models import parse_stage, Stage
from app.runtime.context import RunContext
from app.runtime.stages.report import handle_report
from app.runtime.citations import build_row_trace_url
from test_trace_helpers import write_run


# ── the link shape ────────────────────────────────────────────────────────────

def test_build_row_trace_url_matches_the_trace_view_route():
    assert build_row_trace_url("palm", "20260723T101500", "score_rows", 4) == (
        "/project/palm/runs/20260723T101500/stage/score_rows/row/4/trace/view"
    )


def test_build_row_trace_url_percent_encodes_each_segment():
    url = build_row_trace_url("my project", "a/b", "stage one", 0)
    assert url == "/project/my%20project/runs/a%2Fb/stage/stage%20one/row/0/trace/view"


def test_build_row_trace_url_rejects_a_negative_ordinal():
    with pytest.raises(ValueError):
        build_row_trace_url("palm", "R1", "score_rows", -1)


def test_build_row_trace_url_names_the_cited_column():
    url = build_row_trace_url("palm", "R1", "score_rows", 4, column="mill_score")
    assert url == "/project/palm/runs/R1/stage/score_rows/row/4/trace/view?column=mill_score"


def test_build_row_trace_url_percent_encodes_the_column():
    url = build_row_trace_url("palm", "R1", "score_rows", 0, column="score & rank")
    assert url.endswith("?column=score%20%26%20rank")


# ── what the report handler passes ───────────────────────────────────────────

_CITING_PUBLISH_CODE = """
import pathlib

def transform(df, output_dir, citation_provider):
    rows = [
        "<li><a href='" + citation_provider.cite_row("enrich", i) + "'>"
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


_NAME_COLUMN = [{"name": "name", "type": "str", "nullable": True}]


def _report_stage(code: str, input_columns=_NAME_COLUMN) -> Stage:
    return parse_stage({
        "id": "report",
        "type": "report",
        "description": "Report",
        "inputs": [{"id": "enrich"}],
        "report": {"format": "html_report", "destination": "build/"},
        "signature": {"form": "replaces"},
        "function": {"kind": "inline", "code": "import pandas as pd\n" + code},
    })


_FRAME = pd.DataFrame({"name": ["Alpha", "Beta"]})


def test_handler_passes_a_service_when_the_function_declares_it(tmp_path):
    ctx = RunContext.for_workflow_run(
        run_dir=tmp_path / "run", project_id="palm", run_id="R1",
    )
    result = handle_report(place_stage(_report_stage(_CITING_PUBLISH_CODE)), as_inputs({"enrich": _FRAME}), ctx)
    html = (tmp_path / "run" / "artifacts" / "build" / "index.html").read_text(encoding="utf-8")
    assert "/project/palm/runs/R1/stage/enrich/row/0/trace/view" in html
    assert "/project/palm/runs/R1/stage/enrich/row/1/trace/view" in html
    assert len(rows_of(result)) == 1


def test_handler_leaves_a_function_without_the_keyword_untouched(tmp_path):
    ctx = RunContext.for_workflow_run(
        run_dir=tmp_path / "run", project_id="palm", run_id="R1",
    )
    handle_report(place_stage(_report_stage(_PLAIN_PUBLISH_CODE)), as_inputs({"enrich": _FRAME}), ctx)
    html = (tmp_path / "run" / "artifacts" / "build" / "index.html").read_text(encoding="utf-8")
    assert html == "<p>no links</p>"


def test_handler_fails_loudly_when_a_scopeless_run_cannot_address_a_trace(tmp_path):
    ctx = RunContext.for_stages_outside_a_run(run_dir=tmp_path / "run")
    with pytest.raises(TraceLinksUnavailableError) as exc:
        handle_report(place_stage(_report_stage(_CITING_PUBLISH_CODE)), as_inputs({"enrich": _FRAME}), ctx)
    assert "report" in str(exc.value)


# ── the emitted link resolves against the live app ────────────────────────────

def test_a_link_emitted_into_published_html_resolves(tmp_path, monkeypatch):
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    seeds = pd.DataFrame({"facility_id": ["a", "b"], "name": ["Alpha", "Beta"]})
    enrich = seeds.assign(score=[1, 2])
    run_dir = write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ], run_id="R1")

    ctx = RunContext.for_workflow_run(
        run_dir=run_dir, project_id="proj", run_id="R1",
    )
    enrich_columns = [{"name": "facility_id", "type": "str", "nullable": True}, *_NAME_COLUMN,
                      {"name": "score", "type": "int", "nullable": True}]
    handle_report(
        place_stage(_report_stage(_CITING_PUBLISH_CODE, input_columns=enrich_columns)),
        as_inputs({"enrich": enrich}), ctx)
    html = (run_dir / "artifacts" / "build" / "index.html").read_text(encoding="utf-8")

    workspace.set_projects_dir(tmp_path)
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
