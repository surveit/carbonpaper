"""Publish is frame-level, so a function that ranks its rows before rendering
must still link each claim to ITS OWN provenance. The runtime injects each
row's true on-disk ordinal into the frame, and the exporter reads the ordinal
off the row it is handed rather than from a separately-passed position."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import TraceOrdinalColumnCollision, TraceRowNotStamped
from app.models import Stage
from app.runtime.context import RunContext
from app.runtime.stages.publish import handle_publish
from app.runtime.trace_links import TRACE_ROW_ORDINAL_COLUMN
from test_trace_helpers import write_run

# Ranked output — "top N by spend" — is the normal case for this product.
_SORTING_PUBLISH_CODE = """
import pathlib

def transform(df, output_dir, trace_links):
    path = pathlib.Path(output_dir) / "index.html"
    cards = []
    for row in df.sort_values("spend", ascending=False).to_dict("records"):
        href = trace_links.export_row_trace("enrich", from_file=path, row=row)
        cards.append("<li><a href='" + href + "'>" + str(row["name"]) + "</a></li>")
    path.write_text("<ul>" + "".join(cards) + "</ul>", encoding="utf-8")
    return pd.DataFrame({"path": [str(path)]})
"""

# A row assembled by the author, which carries no stamp for the exporter to read.
_HAND_BUILT_ROW_PUBLISH_CODE = """
import pathlib

def transform(df, output_dir, trace_links):
    path = pathlib.Path(output_dir) / "index.html"
    for row in df.sort_values("spend", ascending=False).to_dict("records"):
        trace_links.export_row_trace(
            "enrich", from_file=path, row={"name": row["name"]})
    return pd.DataFrame({"path": [str(path)]})
"""

# Renders "<name>#<stamp>" so a test can read back the ordinal each row carried.
_STAMP_REPORTING_PUBLISH_CODE = """
import pathlib

def transform(df, output_dir, trace_links):
    path = pathlib.Path(output_dir) / "index.html"
    cards = []
    for row in df.sort_values("spend", ascending=False).to_dict("records"):
        href = trace_links.export_row_trace("enrich", from_file=path, row=row)
        cards.append("<li><a href='" + href + "'>" + str(row["name"])
                     + "#" + str(row["trace_row_ordinal"]) + "</a></li>")
    path.write_text("<ul>" + "".join(cards) + "</ul>", encoding="utf-8")
    return pd.DataFrame({"path": [str(path)]})
"""

_COLUMN_REPORTING_PUBLISH_CODE = """
import pathlib

def transform(df, output_dir):
    path = pathlib.Path(output_dir) / "index.html"
    path.write_text(",".join(df.columns), encoding="utf-8")
    return pd.DataFrame({"path": [str(path)]})
"""

_NAMES = ["Acme", "Beta", "Cyrus", "Delta", "Zeta"]
_SEEDS = pd.DataFrame({"facility_id": list("abcde"), "name": _NAMES})
_ENRICHED = _SEEDS.assign(spend=[10, 50, 20, 40, 30])
# Ranked order (Beta, Delta, Zeta, Cyrus, Acme) shares no position with disk
# order, so a positional mix-up cannot pass by luck.
_RANKED_NAMES = ["Beta", "Delta", "Zeta", "Cyrus", "Acme"]

# What an upstream python_frame_function or human_review_queue hands publish.
# Holes (1..4) make an index-aligned stamp go NaN; a permutation (0..4 reordered)
# makes it stay a valid integer and point at the wrong row — the silent case.
_FILTERED = _ENRICHED[_ENRICHED["spend"] >= 20]
_SHUFFLED = _ENRICHED.sort_values("spend")
_NON_RANGE_FRAMES = [
    pytest.param(_FILTERED, ["Beta", "Cyrus", "Delta", "Zeta"], id="holey"),
    pytest.param(_SHUFFLED, ["Acme", "Cyrus", "Zeta", "Delta", "Beta"], id="permuted"),
]

_ENRICH_STAGE = Stage.model_validate({
    "id": "enrich",
    "type": "python_row_function",
    "name": "Enrich",
    "inputs": [{"id": "seeds"}],
    "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
})


def _publish_stage(code: str) -> Stage:
    return Stage.model_validate({
        "id": "report",
        "type": "publish",
        "name": "Report",
        "inputs": [{"id": "enrich"}],
        "publish": {"format": "html_report", "destination": "build/"},
        "function": {"kind": "inline", "code": "import pandas as pd\n" + code},
    })


@pytest.fixture
def run_dir(tmp_path):
    return _write_run(tmp_path, _ENRICHED)


def _write_run(tmp_path, enriched):
    """`write_run` persists with index=False, mirroring _persist_stage_output —
    so disk row order is the frame's POSITION order whatever its index holds."""
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    return write_run(project_runs, [
        # Row counts must match along the chain or the tracer refuses to cross.
        {"id": "seeds", "type": "input_data", "parents": [],
         "df": enriched[list(_SEEDS.columns)]},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"],
         "df": enriched},
    ], run_id="R1")


def _ctx(tmp_path, run_dir) -> RunContext:
    return RunContext.for_production_run(
        repo_root=tmp_path, run_dir=run_dir, project="proj", run_id="R1",
        stages=[_ENRICH_STAGE],
    )


def test_a_ranked_publish_links_every_card_to_its_own_row(tmp_path, run_dir):
    """The dogfood scenario. Each card's href must resolve to the trace of the
    row that card is about — checked on the page's CONTENT, not its existence."""
    handle_publish(_publish_stage(_SORTING_PUBLISH_CODE), {"enrich": _ENRICHED},
                   _ctx(tmp_path, run_dir))

    published = run_dir / "artifacts" / "build" / "index.html"
    cards = _cards(published.read_text(encoding="utf-8"))
    assert [name for _, name in cards] == _RANKED_NAMES
    for href, name in cards:
        page = (published.parent / href).resolve().read_text(encoding="utf-8")
        assert name in page
        for other in _NAMES:
            if other != name:
                assert other not in page, f"card {name} shows {other}'s provenance"


@pytest.mark.parametrize("frame,disk_order", _NON_RANGE_FRAMES)
def test_the_stamp_counts_positions_not_the_index(tmp_path, frame, disk_order):
    """The parquet is written index=False, so disk row order is the frame's
    POSITION order. An index-aligned stamp would mislabel every row."""
    assert list(frame.index) != list(range(len(frame))), "premise: not a RangeIndex"
    run_dir = _write_run(tmp_path, frame)
    handle_publish(_publish_stage(_STAMP_REPORTING_PUBLISH_CODE), {"enrich": frame},
                   _ctx(tmp_path, run_dir))

    published = run_dir / "artifacts" / "build" / "index.html"
    cards = _cards(published.read_text(encoding="utf-8"))
    stamped = {label.split("#")[0]: int(label.split("#")[1]) for _, label in cards}
    assert stamped == dict(zip(disk_order, range(len(disk_order))))
    for href, label in cards:
        name = label.split("#")[0]
        page = (published.parent / href).resolve().read_text(encoding="utf-8")
        assert name in page
        for other in _NAMES:
            if other != name:
                assert other not in page, f"card {name} shows {other}'s provenance"


def test_a_publish_passing_a_hand_built_row_raises(tmp_path, run_dir):
    """Rendering a row the runtime never stamped leaves nothing to read the
    ordinal from, so the export fails rather than guessing a position."""
    with pytest.raises(TraceRowNotStamped) as excinfo:
        handle_publish(_publish_stage(_HAND_BUILT_ROW_PUBLISH_CODE),
                       {"enrich": _ENRICHED}, _ctx(tmp_path, run_dir))
    message = str(excinfo.value)
    assert "report" in message, "the failure must name the publish stage to fix"
    assert TRACE_ROW_ORDINAL_COLUMN in message


def test_an_input_already_carrying_the_column_raises(tmp_path, run_dir):
    """Overwriting would silently replace the author's own data with ordinals."""
    collided = _ENRICHED.assign(**{TRACE_ROW_ORDINAL_COLUMN: ["x"] * len(_ENRICHED)})
    with pytest.raises(TraceOrdinalColumnCollision) as excinfo:
        handle_publish(_publish_stage(_SORTING_PUBLISH_CODE), {"enrich": collided},
                       _ctx(tmp_path, run_dir))
    message = str(excinfo.value)
    assert TRACE_ROW_ORDINAL_COLUMN in message and "report" in message


def test_a_function_not_asking_for_traces_sees_an_unchanged_frame(tmp_path, run_dir):
    handle_publish(_publish_stage(_COLUMN_REPORTING_PUBLISH_CODE),
                   {"enrich": _ENRICHED}, _ctx(tmp_path, run_dir))

    columns = (run_dir / "artifacts" / "build" / "index.html").read_text(
        encoding="utf-8").split(",")
    assert columns == list(_ENRICHED.columns)


def test_injection_does_not_leak_into_the_frame_the_runner_holds(tmp_path, run_dir):
    """The runner's dict of stage outputs feeds every consumer; injecting in
    place would hand a sibling stage a column its schema never declared."""
    inputs = {"enrich": _ENRICHED}
    handle_publish(_publish_stage(_SORTING_PUBLISH_CODE), inputs, _ctx(tmp_path, run_dir))
    assert TRACE_ROW_ORDINAL_COLUMN not in inputs["enrich"].columns


def _cards(html: str) -> list[tuple[str, str]]:
    return [
        (chunk.split("'", 1)[0], chunk.split(">", 1)[1].split("<", 1)[0])
        for chunk in html.split("href='")[1:]
    ]
