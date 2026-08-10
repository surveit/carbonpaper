"""The review packet: a run exported as an offline folder a fact-checker reads
without this app running."""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pandas as pd
import pytest

import app.services.run as run_service
from app.models.review_guide import ReviewGuideStep
from app.services import versioning, workspace
from app.services.versioning import ReviewGuide
from app.web.review_packet import export_review_packet
from app.web.review_packet.pages import PACKET_MAX_TABLE_ROWS
from app.web.routers.review_packet import _write_zip
from app.web.loading import MAX_TABLE_ROWS
from app.services.review_packet.checksums import compute_sha256

_PROJECT = "proj"


@pytest.fixture(autouse=True)
def _synchronous_background(monkeypatch):
    monkeypatch.setattr(
        run_service, "_run_in_background", lambda target, *args: target(*args)
    )


@pytest.fixture
def project_dir(tmp_path):
    workspace.set_projects_dir(tmp_path)
    return tmp_path / _PROJECT


@pytest.fixture
def exported(project_dir, tmp_path):
    """One finished run of a two-stage project, exported to `packets/`."""
    _make_project(project_dir)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)
    return export_review_packet(_PROJECT, run_id, tmp_path / "packets")


def _make_project(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(
        root / "data" / "items.csv", index=False
    )
    (root / "document.md").write_text("# How we did it\nWe loaded items.\n", encoding="utf-8")
    _write_stage(root, "01_load.json", _load_stage(root))
    _write_stage(root, "02_double.json", _double_stage())


def _write_stage(root, filename, stage):
    (root / "compiled" / filename).write_text(json.dumps(stage), encoding="utf-8")


def _load_stage(root):
    return {
        "id": "load",
        "description": "Load items",
        "type": "input_data",
        "connector": {
            "kind": "file",
            "params": {"path": str(root / "data" / "items.csv"), "format": "csv"},
        },
        "signature": {"form": "replaces", "produces": _COLUMNS},
    }


_VAL_COLUMN = {"name": "val", "type": "int", "nullable": False}
_COLUMNS = [{"name": "name", "type": "str", "nullable": False}, _VAL_COLUMN]


def _double_stage():
    return {
        "id": "double",
        "description": "Double the value",
        "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {"columns": _COLUMNS}}],
        "function": {
            "kind": "inline",
            "summary": "Doubles val and keeps the name unchanged.",
            "corner_cases": [
                {
                    "case": "`val` is blank",
                    "expected": "the step fails rather than treating it as zero",
                }
            ],
            "code": "def transform(row):\n    return {**row, 'val': row['val'] * 2}\n",
        },
        # `name` flows through untouched, so it is neither read nor rewritten.
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [_VAL_COLUMN]}],
            "rewrites": [_VAL_COLUMN],
        },
    }


def _seed_version(root):
    vid = versioning.create_version_from_stages(
        root, [_load_stage(root), _double_stage()], message="seed", reviewer="test"
    ).version_id
    versioning.publish_version(root, vid, reviewer="human")
    return vid


def test_packet_holds_every_stage_output_as_csv(exported):
    """One uncapped CSV per stage that produced output."""
    load = pd.read_csv(exported.root / "data" / "load.csv")
    double = pd.read_csv(exported.root / "data" / "double.csv")
    assert list(load["val"]) == [1, 2]
    assert list(double["val"]) == [2, 4]


def test_packet_keeps_the_raw_file_the_run_wrote(exported):
    """A CSV round trip loses dtypes, so the raw parquet travels too."""
    raw = exported.root / "data" / "raw" / "load.parquet"
    assert raw.is_file()
    assert list(pd.read_parquet(raw)["val"]) == [1, 2]


def test_packet_carries_the_run_records_and_the_workflow(exported):
    """The run records, the methodology prose, and the frozen stages."""
    for name in ("manifest.json", "events.jsonl", "methodology.md", "workflow.json"):
        assert (exported.root / name).is_file(), name
    stages = json.loads((exported.root / "workflow.json").read_text(encoding="utf-8"))
    assert [s["id"] for s in stages] == ["load", "double"]


def test_packet_copies_the_input_file_the_run_read(exported):
    """The bound source file travels with the packet, named by its stage."""
    copies = sorted((exported.root / "inputs").glob("*"))
    assert len(copies) == 1
    assert "load" in copies[0].name
    assert list(pd.read_csv(copies[0])["val"]) == [1, 2]


def test_index_links_every_stage_page(exported):
    index = (exported.root / "index.html").read_text(encoding="utf-8")
    assert 'href="stages/load.html"' in index
    assert 'href="stages/double.html"' in index
    assert (exported.root / "stages" / "load.html").is_file()
    assert (exported.root / "stages" / "double.html").is_file()


def test_stage_page_leads_with_the_summary_and_shows_the_code(exported):
    """Prose first; the code is there to check it against."""
    page = (exported.root / "stages" / "double.html").read_text(encoding="utf-8")
    assert "Doubles val and keeps the name unchanged." in page
    assert "the step fails rather than treating it as zero" in page
    assert "row[&#39;val&#39;] * 2" in page


def test_stage_page_renders_the_output_rows(exported):
    page = (exported.root / "stages" / "double.html").read_text(encoding="utf-8")
    assert "<td>4</td>" in page


# The panel ships an inline <script> whose JS contains href="${...}" template
# literals. Strip script BODIES before scanning markup — but keep the opening
# tag, so a real `<script src="https://cdn...">` is still caught.
_SCRIPT_BODY = re.compile(r"(<script[^>]*>).*?(</script>)", re.DOTALL)


def _markup_of(page) -> str:
    return _SCRIPT_BODY.sub(r"\1\2", page.read_text(encoding="utf-8"))


# The packet reaches the network for exactly one thing: the diagram renderer.
# Everything else — data, records, workflow, panels, styles — is local, so a
# reader can open the folder with the network off and lose only the picture.
# Loading it is a deliberate call (the reader is a data fact-checker at a
# publication, not a source), and SRI is what makes it safe: a substituted file
# fails the hash and does not execute.
_ALLOWED_EXTERNAL = "https://cdn.jsdelivr.net/npm/mermaid@11.16.0/dist/mermaid.min.js"


def test_only_the_diagram_renderer_reaches_the_network(exported):
    """One permitted external URL; anything else means the packet stopped being self-contained."""
    for page in exported.root.rglob("*.html"):
        for url in re.findall(r'(?:href|src)="((?:https?:)?//[^"]*)"', _markup_of(page)):
            assert url == _ALLOWED_EXTERNAL, f"{page.name} reaches {url}"


def test_the_one_external_script_is_pinned_and_hash_checked(exported):
    """No integrity+crossorigin and a compromised CDN executes in the reader's browser."""
    index = (exported.root / "index.html").read_text(encoding="utf-8")
    tag = re.search(r"<script[^>]*cdn\.jsdelivr[^>]*>", index)
    assert tag, "no diagram renderer tag on the index"
    assert 'integrity="sha384-' in tag.group(0)
    assert 'crossorigin="anonymous"' in tag.group(0)
    assert "mermaid@11.16.0" in tag.group(0), "version must be pinned, not floating"


def test_the_diagram_survives_the_link_rotting(exported):
    """A URL is not archival, so the flowchart also travels as text."""
    source = (exported.root / "workflow.mmd").read_text(encoding="utf-8")
    assert "flowchart" in source
    assert "double" in source


def test_pages_reference_no_root_relative_url(exported):
    """A leading `/` resolves against the filesystem root once the folder moves."""
    for page in exported.root.rglob("*.html"):
        assert not re.search(r'(?:href|src)="/', _markup_of(page)), page


def test_vendored_app_stylesheet_pulls_nothing_off_the_network(exported):
    """A packet concatenates app/static/*.css, so an @import in one breaks it offline."""
    css = (exported.root / "assets" / "style.css").read_text(encoding="utf-8")
    for pattern in (r"@import", r"url\(", r"@font-face"):
        assert not re.search(pattern, css), (
            f"a sheet in app/static now contains {pattern!r}; the review packet "
            "concatenates them all and needs the result to resolve with no network"
        )


def test_packet_uses_the_apps_own_visual_vocabulary(exported):
    """A reader's source should recognise the packet as the same product."""
    index = (exported.root / "index.html").read_text(encoding="utf-8")
    assert "assets/style.css" in index
    assert 'class="run-status status-ok"' in index
    assert 'class="stages"' in index


def test_every_referenced_asset_exists_in_the_packet(exported):
    """Each stylesheet and page link resolves to a file that is actually here."""
    for page in exported.root.rglob("*.html"):
        for href in re.findall(r'(?:href|src)="([^"#?]+)"', _markup_of(page)):
            if href.startswith(("http://", "https://", "//")):
                continue  # covered by the two external-URL tests above
            target = (page.parent / href).resolve()
            assert target.exists(), f"{page.name} -> {href}"


def test_checksums_cover_every_file_and_match(exported):
    """`shasum -c checksums.txt` is the reviewer's tamper check."""
    lines = (exported.root / "checksums.txt").read_text(encoding="utf-8").splitlines()
    listed = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines}
    on_disk = {
        p.relative_to(exported.root).as_posix()
        for p in exported.root.rglob("*")
        if p.is_file() and p.name != "checksums.txt"
    }
    assert set(listed) == on_disk
    for relative, digest in listed.items():
        assert compute_sha256(exported.root / relative) == digest


def test_packet_reports_nothing_omitted_for_a_complete_run(exported):
    assert exported.omitted == []


def test_pages_name_the_stage_type_plainly(exported):
    """A repr like "StageType.input_data" has no business on a reader's page."""
    for page in exported.root.rglob("*.html"):
        assert "StageType." not in page.read_text(encoding="utf-8")


def test_index_identifies_an_input_by_name_and_hash_not_by_path(exported, project_dir):
    # Scrubbing the verbatim records instead would make them disagree with the real run.
    author_path = str(project_dir / "data" / "items.csv")
    assert author_path not in (exported.root / "index.html").read_text(encoding="utf-8")
    assert author_path in (exported.root / "manifest.json").read_text(encoding="utf-8")
    assert author_path in (exported.root / "workflow.json").read_text(encoding="utf-8")


def test_index_names_the_input_file_and_its_hash(exported):
    index = (exported.root / "index.html").read_text(encoding="utf-8")
    assert "items.csv" in index
    assert re.search(r"[0-9a-f]{64}", index)


def test_missing_output_file_is_reported_not_skipped(project_dir, tmp_path):
    """A missing output is named on the index, never dropped silently."""
    _make_project(project_dir)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)
    (project_dir / "runs" / run_id / "outputs" / "double.parquet").unlink()

    packet = export_review_packet(_PROJECT, run_id, tmp_path / "packets")

    assert [o.path for o in packet.omitted] == ["data/double.csv"]
    assert "output file missing on disk" in packet.omitted[0].reason
    assert "output file missing on disk" in (packet.root / "index.html").read_text(
        encoding="utf-8"
    )


def test_unreadable_version_is_stated_on_the_stage_page(project_dir, tmp_path, monkeypatch):
    """A missing version is stated, not rendered as a stage with no transform."""
    _make_project(project_dir)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)
    manifest_path = project_dir / "runs" / run_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workflow_version"] = "no-such-version"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    packet = export_review_packet(_PROJECT, run_id, tmp_path / "packets")

    page = (packet.root / "stages" / "double.html").read_text(encoding="utf-8")
    assert "no-such-version" in page
    assert [o.path for o in packet.omitted] == ["workflow.json"]


def test_a_stage_page_never_holds_less_than_a_served_page_would(project_dir, tmp_path):
    # The packet may exceed the served cap but never fall under it.
    assert PACKET_MAX_TABLE_ROWS >= MAX_TABLE_ROWS
    _make_project(project_dir)
    rows = MAX_TABLE_ROWS
    pd.DataFrame(
        {"name": [f"n{i}" for i in range(rows)], "val": list(range(rows))}
    ).to_csv(project_dir / "data" / "items.csv", index=False)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)

    packet = export_review_packet(_PROJECT, run_id, tmp_path / "packets")

    page = (packet.root / "stages" / "double.html").read_text(encoding="utf-8")
    assert f"n{rows - 1}" in page, "the last row was dropped by the served page's cap"


def test_a_capped_stage_page_names_the_true_total_and_points_at_the_csv(
    project_dir, tmp_path
):
    # A truncated table reads as the whole output unless the page says otherwise.
    _make_project(project_dir)
    rows = PACKET_MAX_TABLE_ROWS + 1
    pd.DataFrame(
        {"name": [f"n{i}" for i in range(rows)], "val": list(range(rows))}
    ).to_csv(project_dir / "data" / "items.csv", index=False)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)

    packet = export_review_packet(_PROJECT, run_id, tmp_path / "packets")

    page = (packet.root / "stages" / "double.html").read_text(encoding="utf-8")
    assert f"first {PACKET_MAX_TABLE_ROWS} of {rows:,} rows" in page
    assert 'href="../data/double.csv"' in page
    assert f"n{rows - 1}" not in page, "the cap did not actually truncate"


def test_a_stage_page_offers_the_csv_rather_than_a_link_back_to_itself(exported):
    # The stage page IS the full table in a packet, so "view all rows" has nowhere to go.
    page = (exported.root / "stages" / "double.html").read_text(encoding="utf-8")
    assert "view all rows" not in page
    assert 'href="../data/double.csv"' in page


def test_every_step_stays_reachable_when_no_diagram_is_drawn(project_dir, tmp_path):
    # The diagram is the one networked element, so the step links are the offline route.
    _make_project(project_dir)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)
    manifest_path = project_dir / "runs" / run_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workflow_version"] = "no-such-version"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    packet = export_review_packet(_PROJECT, run_id, tmp_path / "packets")

    index = (packet.root / "index.html").read_text(encoding="utf-8")
    assert 'class="mermaid"' not in index
    assert 'href="stages/load.html"' in index
    assert 'href="stages/double.html"' in index


def test_missing_run_raises_rather_than_writing_an_empty_packet(project_dir, tmp_path):
    from app.core.errors import RunNotFoundError

    _make_project(project_dir)
    with pytest.raises(RunNotFoundError):
        export_review_packet(_PROJECT, "20990101T000000", tmp_path / "packets")


def test_index_carries_the_versions_review_guide(project_dir, tmp_path):
    """The author's own account of what to scrutinise, from `_run_guide.html`."""
    _make_project(project_dir)
    version_id = _seed_version(project_dir)
    versioning.save_version_guide(
        project_dir,
        version_id,
        ReviewGuide(
            project=project_dir.name,
            version_id=version_id,
            steps=[
                ReviewGuideStep(
                    title="Check the doubling",
                    prose="Confirm every `val` is exactly twice its input.",
                    stage_ids=["double"],
                    data_description="Every loaded row, its `val` doubled.",
                )
            ],
            unnarrated=["load"],
        ),
    )
    run_id = run_service.start_run(_PROJECT)

    packet = export_review_packet(_PROJECT, run_id, tmp_path / "packets")

    index = (packet.root / "index.html").read_text(encoding="utf-8")
    assert "Review guide" in index
    assert "Check the doubling" in index
    assert "twice its input" in index
    assert 'href="stages/double.html"' in index


def test_guide_stage_links_reach_the_packets_own_pages(project_dir, tmp_path):
    """A packet chip must navigate, not sit on a `#id` with no panel to load."""
    _make_project(project_dir)
    version_id = _seed_version(project_dir)
    versioning.save_version_guide(
        project_dir,
        version_id,
        ReviewGuide(
            project=project_dir.name,
            version_id=version_id,
            steps=[
                ReviewGuideStep(title="Step", prose="p", stage_ids=["load", "double"],
                                data_description="Every loaded row, its `val` doubled.")
            ],
        ),
    )
    run_id = run_service.start_run(_PROJECT)

    packet = export_review_packet(_PROJECT, run_id, tmp_path / "packets")

    index = (packet.root / "index.html").read_text(encoding="utf-8")
    assert 'data-stage-link="double"' in index
    assert '#double"' not in index


def test_download_route_streams_a_zip_of_the_packet(project_dir):
    """The whole packet, zipped, leaving nothing behind in the project."""
    _make_project(project_dir)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)

    response = _client().get(f"/project/{_PROJECT}/runs/{run_id}/packet.zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = {Path(n).relative_to(f"{_PROJECT}-{run_id}").as_posix()
                 for n in archive.namelist() if not n.endswith("/")}
    assert {"index.html", "manifest.json", "checksums.txt", "data/load.csv"} <= names
    assert not (project_dir / "runs" / run_id / "packet").exists()


def test_download_route_404s_for_a_run_that_does_not_exist(project_dir):
    _make_project(project_dir)
    response = _client().get(f"/project/{_PROJECT}/runs/20990101T000000/packet.zip")
    assert response.status_code == 404


def _client():
    """Imported late: app.main mounts routers at import time."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_the_packet_zip_trades_bytes_for_speed_on_compression(tmp_path):
    # Level 1 over the default 6, so a silent revert shows up as a smaller file.
    root = tmp_path / "packet-root"
    root.mkdir()
    payload = ("registrant,client,amount\n" * 8000).encode()
    (root / "big.csv").write_bytes(payload)
    archive = tmp_path / "a.zip"

    _write_zip(archive, root)

    default_level = io.BytesIO()
    with zipfile.ZipFile(default_level, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.csv", payload)
    with zipfile.ZipFile(archive) as zf:
        entry = zf.infolist()[0]
    assert entry.filename == "packet-root/big.csv"
    assert entry.compress_type == zipfile.ZIP_DEFLATED
    assert entry.compress_size > len(default_level.getvalue())


def test_stage_page_names_its_own_validation_because_no_index_here_does(exported):
    """No index here lists issues, so the stage page holds the only copy."""
    index = (exported.root / "index.html").read_text(encoding="utf-8")
    # The run page's own index is what lets the served panel drop this block.
    assert 'id="run-issues"' not in index

    page = (exported.root / "stages" / "double.html").read_text(encoding="utf-8")
    assert 'class="validation-block"' in page
