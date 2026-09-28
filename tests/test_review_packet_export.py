"""The packet's archive: sources, judgments, spans with their verdicts, the pages they quote."""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core.files import ProjectFile, list_project_files, resolve_stored_path
from app.core.judgments import Judgment
from app.core.text_sources import read_page_text
from app.main import app
from app.models.connectors import (
    CONNECTORS, SOURCE_COLUMNS, AcquiredBytes, ConnectorParams, ConnectorSpec,
)
from app.models.locators import PageCharRange
from app.models.packs import PACKS, PackSpec, register_pack
from app.models.spans import Span
from app.runtime.stages import llm_transform as lt
from app.services import run as run_service
from app.services import uploads
from app.services.methodology import write_methodology
from app.services.review_packet.checksums import compute_sha256
from app.services.versioning import create_version_from_stages
from app.services.workflow_test import run_workflow_test
from app.services.workspace import resolve_project_dir
from app.tools.tutorial import TutorialContext, seed_tutorial_project
from app.web.review_packet import export_review_packet
from conftest import reads_of, script_judgment
from pdf_fixture import write_text_pdf

PROJECT = "docket"
FIRST_PAGE = "The first page says one thing."
SECOND_PAGE = "The second page says another."
QUOTE = "says"
EXAMPLE_ORIGIN = "https://example.org/filings/motion.pdf"
# https://storage.courtlistener.com/recap/gov.uscourts.txnd.342881/gov.uscourts.txnd.342881.58.0.pdf
ECF_58_PAGE_1 = Path(__file__).parent / "fixtures" / "us_v_boeing_ecf58_page1.pdf"
ECF_58_ORIGIN = (
    "https://storage.courtlistener.com/recap/gov.uscourts.txnd.342881/"
    "gov.uscourts.txnd.342881.58.0.pdf")
FREE_LAW_LINE = "Free Law Project did not produce, endorse or verify this run"

_KIND = "one_file"
_SOURCE_COLUMNS = [column.model_dump(mode="json", exclude_defaults=True)
                   for column in SOURCE_COLUMNS]
_NAMING_A_FILE = _SOURCE_COLUMNS[:2]
_PAGE_SPAN = {"name": "page_span", "type": "span", "nullable": False}
_PAGE_COLUMNS = [{"name": "page", "type": "int", "nullable": False},
                 {"name": "page_text", "type": "str", "nullable": False}, _PAGE_SPAN]


class _OneFileParams(ConnectorParams):
    file: str
    origin_url: str


def _acquire_one_file(params: _OneFileParams) -> Iterator[AcquiredBytes]:
    path = Path(params.file)
    yield AcquiredBytes(filename=path.name, origin_url=params.origin_url,
                        open_bytes=lambda: path.open("rb"), metadata={})


@pytest.fixture
def one_file_pack() -> Iterator[None]:
    saved_connectors, saved_packs = dict(CONNECTORS), dict(PACKS)
    register_pack(PackSpec(pack_id="archive_test", connectors=(ConnectorSpec(
        kind=_KIND, params_model=_OneFileParams, metadata_columns=(),
        acquire=_acquire_one_file),)))
    yield
    CONNECTORS.clear()
    CONNECTORS.update(saved_connectors)
    PACKS.clear()
    PACKS.update(saved_packs)


def _quoting_stages(pdf: Path, origin_url: str) -> list[dict[str, Any]]:
    return [
        {"id": "filings", "description": "Read the filing", "type": "input_data",
         "connector": {"kind": _KIND, "params": {"file": str(pdf), "origin_url": origin_url}},
         "signature": {"form": "replaces", "produces": _SOURCE_COLUMNS}},
        {"id": "pages", "description": "Read the filing a page at a time", "type": "read_pages",
         "inputs": [{"id": "filings"}], "row_type_id": "filing_page",
         "signature": {"form": "replaces", "reads": reads_of("filings", _NAMING_A_FILE),
                       "produces": [*_NAMING_A_FILE, *_PAGE_COLUMNS]},
         "read_pages": {"carry": []}},
        {"id": "quote", "description": "Quote each page", "type": "llm_transform",
         "inputs": [{"id": "pages"}],
         "signature": {"form": "extends", "reads": reads_of("pages", [_PAGE_SPAN]),
                       "adds": [{"name": "basis", "type": "span", "nullable": True,
                                 "quoted_from": "page_span"}]},
         "llm": {"prompt_data_template": "Quote the verb:\n{page_span}"},
         "workflow_outputs": [{"kind": "table", "slug": "quotes", "label": "Quotes",
                               "columns": ["page", "basis"]}]},
    ]


def _run_quoting(
    monkeypatch: pytest.MonkeyPatch, pdf: Path, origin_url: str,
    quote_from: Callable[[str], str | None],
) -> str:
    """The model is shown each page's text; `quote_from` is its reply, None a null basis."""
    resolve_project_dir(PROJECT).mkdir(parents=True, exist_ok=True)
    write_methodology(PROJECT, "Quote each page of the filing.")
    create_version_from_stages(PROJECT, _quoting_stages(pdf, origin_url), message="v1")

    def answer(stage_id: str, llm_config: object, row: dict[str, Any], **kw: object) -> object:
        quote = quote_from(row["page_span"])
        return script_judgment({"basis": None if quote is None else {"quote": quote}})

    monkeypatch.setattr(lt, "call_llm", answer)
    manifest = run_service.execute(PROJECT)
    assert [record["status"] for record in manifest["stage_records"]] == ["ok"] * 3
    return str(manifest["run_id"])


@pytest.fixture
def two_pages(tmp_path: Path) -> Path:
    return write_text_pdf(tmp_path / "motion.pdf", [FIRST_PAGE, SECOND_PAGE])


@pytest.fixture
def quoted_run(projects_root, one_file_pack, monkeypatch, two_pages) -> str:
    return _run_quoting(monkeypatch, two_pages, EXAMPLE_ORIGIN,
                        lambda page_text: QUOTE if page_text == SECOND_PAGE else None)


@pytest.fixture
def quoted_packet(quoted_run, tmp_path) -> Path:
    return export_review_packet(PROJECT, quoted_run, tmp_path / "packets").root


def _stored_pdf() -> ProjectFile:
    [record] = list_project_files(PROJECT)
    return record


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── a run that read a PDF and published a span column ────────────────────────


def test_sources_json_names_each_file_the_run_read_and_its_copy_here(quoted_packet):
    stored = _stored_pdf()

    [source] = _read_json(quoted_packet / "sources.json")

    assert source == {
        "id": stored.id, "filename": "motion.pdf", "sha256": stored.sha256,
        "origin_url": EXAMPLE_ORIGIN, "fetched_at": stored.fetched_at,
        "packet_path": "inputs/00-filings.pdf",
    }
    assert compute_sha256(quoted_packet / source["packet_path"]) == stored.sha256


def test_judgments_jsonl_holds_one_line_per_model_call(quoted_packet):
    lines = (quoted_packet / "judgments.jsonl").read_text(encoding="utf-8").splitlines()

    recorded = [Judgment.model_validate_json(line) for line in lines]
    assert sorted(judgment.id for judgment in recorded) == sorted(
        judgment.id for judgment in Judgment.find(project_id=PROJECT))
    assert sorted(str(judgment.reply) for judgment in recorded) == [
        "{'basis': None}", "{'basis': {'quote': 'says'}}"]


def test_spans_json_gives_each_published_span_the_verifiers_verdict(quoted_packet):
    [entry] = _read_json(quoted_packet / "spans.json")

    assert (entry["stage_id"], entry["row"], entry["column"], entry["refusal"]) == (
        "quote", 1, "basis", None)
    assert Span.model_validate(entry["span"]).quote == QUOTE


def test_the_verified_span_s_page_is_written_where_its_offsets_hold(quoted_packet, two_pages):
    [entry] = _read_json(quoted_packet / "spans.json")
    locator = Span.model_validate(entry["span"]).locator
    assert isinstance(locator, PageCharRange) and locator.page == 2
    pages = quoted_packet / "sources" / _stored_pdf().id / "pages"

    with (pages / "2.txt").open(encoding="utf-8", newline="") as written:
        text = written.read()

    assert text == read_page_text(two_pages, 2)
    assert text[locator.start:locator.end] == QUOTE
    assert not (pages / "1.txt").exists(), "no published span quotes page 1"


def test_a_stage_page_links_only_the_page_text_the_packet_holds(quoted_packet):
    page_2 = f"../sources/{_stored_pdf().id}/pages/2.txt"

    quoted = (quoted_packet / "stages" / "quote.html").read_text(encoding="utf-8")
    read = (quoted_packet / "stages" / "pages.html").read_text(encoding="utf-8")

    assert f'<a href="{page_2}">page 2</a>' in quoted
    assert f'href="{page_2}"' in read and "pages/1.txt" not in read
    links = [(page, href) for page in quoted_packet.rglob("*.html")
             for href in re.findall(r'href="([^"]*sources/[^"]*)"', page.read_text(encoding="utf-8"))]
    assert links and all((page.parent / href).is_file() for page, href in links)


def test_a_stage_csv_is_the_csv_the_app_serves(quoted_run, quoted_packet):
    served = TestClient(app).get(f"/project/{PROJECT}/runs/{quoted_run}/stage/quote/rows.csv")

    written = (quoted_packet / "data" / "quote.csv").read_text(encoding="utf-8")
    assert written == served.content.decode("utf-8-sig")
    assert "'page': 2," in written, "a null row beside it leaves the offsets ints"


def test_each_stage_s_lineage_sidecar_sits_beside_its_raw_output(quoted_packet):
    raw = quoted_packet / "data" / "raw"

    assert (raw / "filings.lineage.parquet").is_file()
    assert (raw / "pages.lineage.parquet").is_file()


def test_the_checksums_cover_every_archive_file(quoted_packet):
    checksums = (quoted_packet / "checksums.txt").read_text(encoding="utf-8").splitlines()
    listed = {line.split("  ", 1)[1] for line in checksums}
    on_disk = {path.relative_to(quoted_packet).as_posix()
               for path in quoted_packet.rglob("*")
               if path.is_file() and path.name != "checksums.txt"}

    assert listed == on_disk
    assert {"sources.json", "judgments.jsonl", "spans.json", "terms.json", "methodology.md",
            f"sources/{_stored_pdf().id}/pages/2.txt", "data/raw/pages.lineage.parquet",
            } <= listed


def test_the_index_links_each_archive_file_and_owes_no_attribution(quoted_packet):
    index = (quoted_packet / "index.html").read_text(encoding="utf-8")

    for name in ("sources.json", "judgments.jsonl", "spans.json", "terms.json",
                 "methodology.md"):
        assert f'href="{name}"' in index
    assert "sources/&lt;id&gt;/pages/&lt;page&gt;.txt" in index
    assert FREE_LAW_LINE not in index


def test_a_span_whose_file_changed_since_the_run_is_refused_and_quotes_no_page(
    quoted_run, tmp_path,
):
    write_text_pdf(resolve_stored_path(_stored_pdf()), ["Rewritten.", "Rewritten again."])

    packet = export_review_packet(PROJECT, quoted_run, tmp_path / "packets")

    [entry] = _read_json(packet.root / "spans.json")
    assert "motion.pdf now hashes to" in entry["refusal"]
    assert not (packet.root / "sources").exists()


def test_a_file_from_courtlistener_puts_the_free_law_project_line_on_the_index(
    projects_root, one_file_pack, monkeypatch, tmp_path,
):
    run_id = _run_quoting(monkeypatch, ECF_58_PAGE_1, ECF_58_ORIGIN,
                          lambda page_text: "FORT WORTH DIVISION")

    packet = export_review_packet(PROJECT, run_id, tmp_path / "packets")

    index = (packet.root / "index.html").read_text(encoding="utf-8")
    assert FREE_LAW_LINE in index
    [source] = _read_json(packet.root / "sources.json")
    assert source["origin_url"] == ECF_58_ORIGIN and source["fetched_at"] is not None


# ── a test run, which records no judgment ───────────────────────────────────


_X = [{"name": "x", "type": "int", "nullable": False}]


def test_a_test_run_s_model_rows_leave_judgments_jsonl_omitted_with_the_reason(
    projects_root, monkeypatch, tmp_path,
):
    rows = resolve_project_dir(PROJECT) / "rows.csv"
    rows.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"x": [1, 2]}).to_csv(rows, index=False)
    create_version_from_stages(PROJECT, [
        {"id": "load", "description": "Load rows", "type": "input_data",
         "connector": {"kind": "file", "params": {"path": str(rows), "format": "csv"}},
         "signature": {"form": "replaces", "produces": _X}},
        {"id": "judge", "description": "Judge each row", "type": "llm_transform",
         "inputs": [{"id": "load"}],
         "signature": {"form": "extends", "reads": reads_of("load", _X),
                       "adds": [{"name": "verdict", "type": "str", "nullable": True}]},
         "llm": {"prompt_data_template": "Rate: {x}"}},
    ], message="v1")
    monkeypatch.setattr(lt, "call_llm", lambda stage_id, llm_config, row, **kw: script_judgment(
        {"verdict": "fine"}))
    tested = run_workflow_test(PROJECT)
    assert tested["ok"], tested["error"]

    packet = export_review_packet(PROJECT, tested["run_id"], tmp_path / "packets")

    assert not (packet.root / "judgments.jsonl").exists()
    [omitted] = [o for o in packet.omitted if o.path == "judgments.jsonl"]
    assert omitted.reason == (
        "stage 'judge': 2 row(s) were decided in a test run, which records no judgment")


# ── the tour's capped run: no span column, cached judgments ──────────────────


_TOUR_CAP = 50


def test_the_tour_s_capped_run_archives_its_files_and_says_why_it_holds_no_judgment(
    projects_root, tmp_path,
):
    tour = seed_tutorial_project(TutorialContext(base_url="http://127.0.0.1:8788/"))
    bindings = {stage_id: uploads.resolve_files_binding(tour.project.id, file_ids)
                for stage_id, file_ids in tour.input_files.items()}
    run_id = str(run_service.execute(
        tour.project.id, bindings=bindings, limits={"input_filings": _TOUR_CAP})["run_id"])

    packet = export_review_packet(tour.project.id, run_id, tmp_path / "packets")

    stored = {record.id: record for record in list_project_files(tour.project.id)}
    sources = _read_json(packet.root / "sources.json")
    assert {source["id"] for source in sources} == {
        file_id for file_ids in tour.input_files.values() for file_id in file_ids}
    assert all(source["sha256"] == stored[source["id"]].sha256 for source in sources)
    assert not (packet.root / "judgments.jsonl").exists()
    [omitted] = [o for o in packet.omitted if o.path == "judgments.jsonl"]
    assert omitted.reason.startswith("stage 'judge_ai_substance': ")
    assert omitted.reason.endswith(
        "row(s) were replayed from cache entries recorded before judgments were kept")
    assert not (packet.root / "spans.json").exists()
    assert not (packet.root / "sources").exists()
    assert (packet.root / "data" / "raw" / "input_filings.lineage.parquet").is_file()
    assert (packet.root / "terms.json").is_file()
