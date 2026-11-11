"""recap_files against a local server and a local mirror, never against CourtListener."""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import url_fetch
from app.core.errors import MissingInputBindingError
from app.core.files import list_project_files
from app.core.frames import read_frame_table
from app.models.connectors import SOURCE_COLUMNS
from app.models.run_manifest import index_bound_sources
from app.models.spans import Span
from app.packs.docket import files
from app.packs.docket.files import RecapFilesParams
from app.packs.docket.mirror import MIRROR_MANIFEST
from app.runtime.runner import execute_run
from app.runtime.spans import SourceTextCache, verify_span
from conftest import pinned_stages, reads_of
from pdf_fixture import write_text_pdf
from stage_seed import save_version, set_stages

DAY_1_PATH = "recap/gov.uscourts.mowd.91111/gov.uscourts.mowd.91111.80.0.pdf"
DAY_2_PATH = "recap/gov.uscourts.mowd.91111/gov.uscourts.mowd.91111.81.0.pdf"
DAY_1_TEXT = "THE COURT: Good morning, members of the jury."
DAY_2_TEXT = "THE COURT: The jury may retire to deliberate."
MIRROR_FETCHED_AT = "2026-11-11T18:00:00Z"


def _file(path: str, ecf: str, part: int) -> dict[str, object]:
    return {"path": path, "court": "mowd", "case_number": "3:09-cr-05022",
            "case_name": "United States v. Joos", "docket_id": 30527442, "ecf": ecf,
            "part": part, "description": f"Criminal Jury Trial - Day {part}"}


def _params(**extra: object) -> dict[str, object]:
    return {"files": [_file(DAY_1_PATH, "80", 1), _file(DAY_2_PATH, "81", 2)], **extra}


_METADATA = [column.model_dump(mode="json", exclude_defaults=True)
             for column in files.RECAP_FILES.metadata_columns]
_SOURCE = [column.model_dump(mode="json", exclude_defaults=True) for column in SOURCE_COLUMNS]
_READ = [column for column in _SOURCE if column["name"] in ("source_id", "source_sha256")]
_PART = [column for column in _METADATA if column["name"] == "part"]
_PAGES = [{"name": "page", "type": "int", "nullable": False},
          {"name": "page_text", "type": "str", "nullable": False},
          {"name": "page_span", "type": "span", "nullable": False}]


def _run_files(tmp_path: Path, params: dict[str, object]) -> dict:
    set_stages(tmp_path, [
        {"id": "volumes", "description": "Read the transcript files", "type": "input_data",
         "row_type_id": "volume", "connector": {"kind": "recap_files", "params": params},
         "signature": {"form": "replaces", "produces": [*_SOURCE, *_METADATA]}},
        {"id": "volume_pages", "description": "Read each file a page at a time",
         "type": "read_pages", "inputs": [{"id": "volumes"}], "row_type_id": "volume_page",
         "signature": {"form": "replaces", "reads": reads_of("volumes", [*_READ, *_PART]),
                       "produces": [*_READ, *_PART, *_PAGES]},
         "read_pages": {"carry": ["part"]}},
    ])
    save_version(tmp_path.name, message="seed")
    return execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path))


def _read_output(tmp_path: Path, manifest: dict, stage_id: str) -> list[dict]:
    return read_frame_table(
        tmp_path / "runs" / manifest["run_id"] / "outputs" / f"{stage_id}.parquet").to_pylist()


def _verify_page_spans(manifest: dict, pages: list[dict]) -> None:
    bound = index_bound_sources(manifest["input_bindings"])
    texts = SourceTextCache()
    for page in pages:
        verify_span(Span.model_validate(page["page_span"]), bound, texts)


@pytest.fixture
def storage(tmp_path: Path, monkeypatch) -> Iterator[list[tuple[str, str]]]:
    served = {DAY_1_PATH: write_text_pdf(tmp_path / "d1.pdf", [DAY_1_TEXT]).read_bytes(),
              DAY_2_PATH: write_text_pdf(tmp_path / "d2.pdf", [DAY_2_TEXT]).read_bytes()}
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get("User-Agent", "")))
            body = served.get(self.path.lstrip("/"))
            self.send_response(404 if body is None else 200)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    monkeypatch.setattr(files, "RECAP_STORAGE", f"http://127.0.0.1:{server.server_port}/")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield requests
    server.shutdown()
    server.server_close()


def test_a_run_stores_each_named_file_with_the_case_facts_its_workflow_states(
    storage, tmp_path,
) -> None:
    manifest = _run_files(tmp_path, _params())

    assert manifest["status"] == "ok"
    volumes = _read_output(tmp_path, manifest, "volumes")
    assert [(row["filename"], row["ecf_entry"], row["part"]) for row in volumes] == [
        ("mowd-3-09-cr-05022_ecf0080.pdf", 80, 1), ("mowd-3-09-cr-05022_ecf0081.pdf", 81, 2)]
    assert volumes[0]["case_name"] == "United States v. Joos"
    assert volumes[0]["docket_id"] == 30527442
    pages = _read_output(tmp_path, manifest, "volume_pages")
    assert [(page["part"], page["page_text"]) for page in pages] == [
        (1, DAY_1_TEXT), (2, DAY_2_TEXT)]
    _verify_page_spans(manifest, pages)
    assert {agent for _, agent in storage} == {files.USER_AGENT}


@pytest.fixture
def offline(monkeypatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a mirror read reached for the network")

    monkeypatch.setattr(url_fetch, "urlopen", refuse)


def _write_mirror(root: Path, recorded_sha256: str | None = None) -> Path:
    root.mkdir()
    lines = []
    for name, path, text in (("d1.pdf", DAY_1_PATH, DAY_1_TEXT), ("d2.pdf", DAY_2_PATH, DAY_2_TEXT)):
        pdf = write_text_pdf(root / name, [text])
        lines.append({"path": name, "source_url": f"{files.RECAP_STORAGE}{path}",
                      "fetched_at": MIRROR_FETCHED_AT,
                      "sha256": recorded_sha256 or hashlib.sha256(pdf.read_bytes()).hexdigest()})
    (root / MIRROR_MANIFEST).write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return root


def test_a_mirror_supplies_the_bytes_and_the_time_they_were_fetched(offline, tmp_path) -> None:
    mirror = _write_mirror(tmp_path / "mirror")

    manifest = _run_files(tmp_path, _params(cache_dir=str(mirror)))

    assert manifest["status"] == "ok"
    stored = list_project_files(tmp_path.name)
    assert sorted(record.origin_url for record in stored) == [
        f"https://storage.courtlistener.com/{DAY_1_PATH}",
        f"https://storage.courtlistener.com/{DAY_2_PATH}"]
    _verify_page_spans(manifest, _read_output(tmp_path, manifest, "volume_pages"))


def test_a_mirrored_file_off_its_recorded_sha256_is_refused(offline, tmp_path) -> None:
    mirror = _write_mirror(tmp_path / "mirror", recorded_sha256="0" * 64)
    with pytest.raises(MissingInputBindingError, match=f"its mirror recorded {'0' * 64}"):
        _run_files(tmp_path, _params(cache_dir=str(mirror)))
    assert list_project_files(tmp_path.name) == []


@pytest.mark.parametrize("named, refusal", [
    ([_file(DAY_1_PATH, "80", 1), _file(DAY_1_PATH, "81", 2)], "more than once"),
    ([_file(DAY_1_PATH, "80", 1), _file(DAY_2_PATH, "81", 1)], "same part"),
    ([], "at least 1"),
    ([{**_file(DAY_1_PATH, "80", 1), "path": "https://example.com/a.pdf"}], "pattern"),
])
def test_params_refuse_a_file_named_twice_or_off_the_archive(
    named: list[dict[str, object]], refusal: str,
) -> None:
    with pytest.raises(ValidationError, match=refusal):
        RecapFilesParams.model_validate({"files": named})
