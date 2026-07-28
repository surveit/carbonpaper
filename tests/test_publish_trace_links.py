"""Published output carries each row's provenance WITH it: the publish handler
hands a declaring function a RowTraceExporter, which writes the trace page into
the artifact bundle and returns an href relative to the file being written."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import TraceUnavailableError
from app.models import Stage
from app.runtime.context import RunContext
from app.runtime.stages.publish import handle_publish
from test_trace_helpers import write_run

_EXPORTING_PUBLISH_CODE = """
import pathlib

def transform(df, output_dir, trace_links):
    path = pathlib.Path(output_dir) / "index.html"
    rows = [
        "<li><a href='"
        + trace_links.export_row_trace("enrich", i, from_file=path)
        + "'>" + str(row["name"]) + "</a></li>"
        for i, row in enumerate(df.to_dict("records"))
    ]
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


def _publish_stage(code: str, input_id: str = "enrich") -> Stage:
    return Stage.model_validate({
        "id": "report",
        "type": "publish",
        "name": "Report",
        "inputs": [{"id": input_id}],
        "publish": {"format": "html_report", "destination": "build/"},
        "function": {"kind": "inline", "code": "import pandas as pd\n" + code},
    })


_ENRICH_STAGE = Stage.model_validate({
    "id": "enrich",
    "type": "python_row_function",
    "name": "Enrich",
    "inputs": [{"id": "seeds"}],
    "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
})

_SEEDS = pd.DataFrame({"facility_id": ["a", "b"], "name": ["Alpha", "Beta"]})
_ENRICHED = _SEEDS.assign(score=[1, 2])


@pytest.fixture
def run_dir(tmp_path):
    """A real run directory — outputs + manifest — for the tracer to walk."""
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    return write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": _SEEDS},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"],
         "df": _ENRICHED},
    ], run_id="R1")


def _production_ctx(tmp_path, run_dir, stages: list[Stage]) -> RunContext:
    return RunContext.for_production_run(
        repo_root=tmp_path, run_dir=run_dir, project="proj", run_id="R1", stages=stages,
    )


# ── the published file links to a page that is really beside it ───────────────

def test_a_published_file_links_to_a_trace_that_exists_beside_it(tmp_path, run_dir):
    ctx = _production_ctx(tmp_path, run_dir, [_ENRICH_STAGE])
    result = handle_publish(
        _publish_stage(_EXPORTING_PUBLISH_CODE), {"enrich": _ENRICHED}, ctx)

    published = run_dir / "artifacts" / "build" / "index.html"
    html = published.read_text(encoding="utf-8")
    assert len(result) == 1
    hrefs = _hrefs(html)
    assert len(hrefs) == 2
    for href in hrefs:
        assert not href.startswith("/"), "a root-relative href needs this app to serve it"
        assert "://" not in href
        assert (published.parent / href).resolve().is_file()


def test_the_exported_page_carries_the_compiled_stage_transform(tmp_path, run_dir):
    """The exporter renders each upstream stage's transform onto the page, which
    it can only do from the stages the run context carries."""
    ctx = _production_ctx(tmp_path, run_dir, [_ENRICH_STAGE])
    handle_publish(_publish_stage(_EXPORTING_PUBLISH_CODE), {"enrich": _ENRICHED}, ctx)

    published = run_dir / "artifacts" / "build" / "index.html"
    page = (published.parent / _hrefs(published.read_text(encoding="utf-8"))[0]).resolve()
    assert "def transform(row)" in page.read_text(encoding="utf-8")


def test_each_row_gets_its_own_page(tmp_path, run_dir):
    ctx = _production_ctx(tmp_path, run_dir, [_ENRICH_STAGE])
    handle_publish(_publish_stage(_EXPORTING_PUBLISH_CODE), {"enrich": _ENRICHED}, ctx)

    traces = run_dir / "artifacts" / "build" / "_traces" / "enrich"
    assert sorted(p.name for p in traces.iterdir()) == ["0.html", "1.html"]


# ── opting out, and failing loudly ────────────────────────────────────────────

def test_handler_leaves_a_function_without_the_keyword_untouched(tmp_path, run_dir):
    ctx = _production_ctx(tmp_path, run_dir, [_ENRICH_STAGE])
    handle_publish(_publish_stage(_PLAIN_PUBLISH_CODE), {"enrich": _ENRICHED}, ctx)

    html = (run_dir / "artifacts" / "build" / "index.html").read_text(encoding="utf-8")
    assert html == "<p>no links</p>"
    assert not (run_dir / "artifacts" / "build" / "_traces").exists()


def test_a_run_with_no_run_dir_fails_loudly(tmp_path):
    """No run directory means no run outputs to trace and nowhere to write —
    the handler raises rather than exporting against a fabricated path."""
    ctx = RunContext.for_non_production_run(repo_root=tmp_path, run_dir=None, stages=[])
    with pytest.raises(ValueError, match="no run_dir"):
        handle_publish(_publish_stage(_EXPORTING_PUBLISH_CODE), {"enrich": _ENRICHED}, ctx)


def test_a_row_whose_lineage_crosses_a_join_raises(tmp_path):
    """A trace that cannot reach the source is unavailable, not partial — so a
    publish function exporting every row of a post-join stage fails the run."""
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    joined = _SEEDS.assign(extra=["x", "y"])
    run_dir = write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": _SEEDS},
        {"id": "other", "type": "input_data", "parents": [], "df": _SEEDS},
        {"id": "enrich", "type": "join", "parents": ["seeds", "other"], "df": joined},
    ], run_id="R1")
    ctx = _production_ctx(tmp_path, run_dir, [])

    with pytest.raises(TraceUnavailableError) as exc:
        handle_publish(_publish_stage(_EXPORTING_PUBLISH_CODE), {"enrich": joined}, ctx)
    # the failure names the publish stage to fix, not only the traced stage/row
    assert "report" in str(exc.value)
    assert "enrich" in str(exc.value)


def _hrefs(html: str) -> list[str]:
    return [chunk.split("'", 1)[0] for chunk in html.split("href='")[1:]]
