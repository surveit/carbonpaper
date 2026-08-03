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
from app.models.review_guide import ReviewGuide, ReviewGuideStep
from app.services import versioning, workspace
from app.web.export import export_review_packet
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
        "name": "Load items",
        "type": "input_data",
        "connector": {
            "kind": "file",
            "params": {"path": str(root / "data" / "items.csv"), "format": "csv"},
        },
        "output_schema": {"columns": _COLUMNS},
    }


_COLUMNS = [{"name": "name", "type": "str"}, {"name": "val", "type": "int"}]


def _double_stage():
    return {
        "id": "double",
        "name": "Double the value",
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
        "output_schema": {"columns": _COLUMNS},
    }


def _seed_version(root):
    vid = versioning.create_version_from_disk(root, message="seed", reviewer="test").version_id
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


def test_pages_reference_no_network_url(exported):
    """The packet opens from disk, so nothing may resolve against a host."""
    for page in exported.root.rglob("*.html"):
        assert not re.search(r'(?:href|src)="(?:https?:)?//', _markup_of(page)), page


def test_pages_reference_no_root_relative_url(exported):
    """A leading `/` resolves against the filesystem root once the folder moves."""
    for page in exported.root.rglob("*.html"):
        assert not re.search(r'(?:href|src)="/', _markup_of(page)), page


def test_vendored_app_stylesheet_pulls_nothing_off_the_network(exported):
    """A packet vendors style.css, so an @import there breaks offline rendering."""
    css = (exported.root / "assets" / "style.css").read_text(encoding="utf-8")
    for pattern in (r"@import", r"url\(", r"@font-face"):
        assert not re.search(pattern, css), (
            f"app/static/style.css now contains {pattern!r}; the review packet "
            "vendors it and needs it to resolve with no network"
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
    """The hash identifies the bytes; only the verbatim records keep the path."""
    # Scrubbing the records would make the packet's workflow disagree with the real
    # one — a worse failure than disclosing the author's directory layout.
    author_path = str(project_dir / "data" / "items.csv")
    assert author_path not in (exported.root / "index.html").read_text(encoding="utf-8")
    assert author_path in (exported.root / "manifest.json").read_text(encoding="utf-8")
    assert author_path in (exported.root / "workflow.json").read_text(encoding="utf-8")


def test_index_names_the_input_file_and_its_hash(exported):
    index = (exported.root / "index.html").read_text(encoding="utf-8")
    assert "items.csv" in index
    assert re.search(r"[0-9a-f]{64}", index)


def test_index_states_the_cache_caveat(exported):
    """A cache-filled row's prompt is absent from events.jsonl; the packet says so."""
    index = (exported.root / "index.html").read_text(encoding="utf-8")
    assert "stage cache" in index


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
            steps=[
                ReviewGuideStep(
                    title="Check the doubling",
                    prose="Confirm every `val` is exactly twice its input.",
                    stage_ids=["double"],
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
            steps=[
                ReviewGuideStep(title="Step", prose="p", stage_ids=["load", "double"])
            ]
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
