"""A text Source's file page: page N's text, the characters a span's link names marked."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.files import ProjectFile, receive_source, save_upload
from app.core.text_sources import read_page_text
from app.main import app
from app.services.project import create_project
from pdf_fixture import write_text_pdf

client = TestClient(app)

PAGES = ["The government moves to dismiss.", "The families oppose the motion.",
         "The court sets a hearing."]
# https://storage.courtlistener.com/recap/gov.uscourts.txnd.342881/gov.uscourts.txnd.342881.58.0.pdf
ECF_58_PAGE_1 = Path(__file__).parent / "fixtures" / "us_v_boeing_ecf58_page1.pdf"
ECF_58_ORIGIN = ("https://storage.courtlistener.com/recap/gov.uscourts.txnd.342881/"
                 "gov.uscourts.txnd.342881.58.0.pdf")


@pytest.fixture
def project_id() -> str:
    return create_project("docket", "A methodology.", source="test").id


def _store(project_id: str, path: Path) -> ProjectFile:
    with path.open("rb") as stream:
        return save_upload(path.name, stream, project_id)


def _get(project_id: str, record: ProjectFile, query: str = "") -> str:
    response = client.get(f"/project/{project_id}/files/{record.id}{query}")
    assert response.status_code == 200, response.text
    return response.text


def _head_of(page: str) -> str:
    start = page.index('<div class="sec-head file-head">')
    return page[start:page.index("</div>\n\n", start)]


def test_the_named_range_of_the_named_page_is_marked(project_id, tmp_path):
    record = _store(project_id, write_text_pdf(tmp_path / "ecf_17.pdf", PAGES))
    start = PAGES[1].index("oppose")

    page = _get(project_id, record, f"?page=2&start={start}&end={start + len('oppose')}")

    assert "Page 2 of 3" in page
    assert "The families <mark>oppose</mark> the motion." in page
    assert PAGES[0] not in page


def test_a_page_links_the_pages_either_side_and_the_bytes_at_itself(project_id, tmp_path):
    record = _store(project_id, write_text_pdf(tmp_path / "ecf_17.pdf", PAGES))
    here = f"/project/{project_id}/files/{record.id}"

    page = _get(project_id, record, "?page=2")

    assert f'href="{here}?page=1"' in page
    assert f'href="{here}?page=3"' in page
    assert f'href="{here}/bytes#page=2"' in page
    assert "<mark>" not in page


def test_the_first_and_last_pages_link_no_page_past_the_ends(project_id, tmp_path):
    record = _store(project_id, write_text_pdf(tmp_path / "ecf_17.pdf", PAGES))
    here = f"/project/{project_id}/files/{record.id}"

    assert f"{here}?page=0" not in _get(project_id, record, "?page=1")
    assert f"{here}?page=4" not in _get(project_id, record, "?page=3")


def test_a_docket_page_shows_its_ecf_stamp_beside_the_marked_quote(project_id):
    with ECF_58_PAGE_1.open("rb") as stream:
        record = receive_source(project_id, ECF_58_ORIGIN, "ecf_58.pdf", stream)
    quote = "IN VIOLATION OF THE CRIME VICTIMS’ RIGHTS ACT"
    start = read_page_text(ECF_58_PAGE_1, 1).index(quote)

    page = _get(project_id, record, f"?page=1&start={start}&end={start + len(quote)}")

    assert f"<mark>{quote}</mark>" in page
    assert "Case 4:21-cr-00005-O   Document 58   Filed 02/08/22    Page 1 of 26" in page
    head = _head_of(page)
    assert record.sha256 in head
    assert ECF_58_ORIGIN in head
    assert "fetched" in head


def test_a_range_past_the_end_of_the_page_says_so_and_marks_nothing(project_id, tmp_path):
    record = _store(project_id, write_text_pdf(tmp_path / "ecf_17.pdf", PAGES))

    page = _get(project_id, record, "?page=3&start=4&end=400")

    assert "<mark>" not in page
    assert (f"Characters 4–400\n    are not on this page, which holds {len(PAGES[2])}, "
            "so nothing is marked.") in page
    assert PAGES[2] in page


def test_the_page_text_is_escaped_rather_than_rendered(project_id, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("Before <script>alert(1)</script> after.", encoding="utf-8")
    record = _store(project_id, notes)

    page = _get(project_id, record, "?page=1&start=7&end=15")

    assert "Before <mark>&lt;script&gt;</mark>alert(1)&lt;/script&gt; after." in page


def test_a_page_with_no_text_layer_says_nothing_on_it_can_be_quoted(project_id, tmp_path):
    record = _store(project_id, write_text_pdf(tmp_path / "exhibit_a.pdf", [""]))

    assert "This page has no text layer" in _get(project_id, record)


def test_a_page_the_file_does_not_have_is_not_found(project_id, tmp_path):
    record = _store(project_id, write_text_pdf(tmp_path / "ecf_17.pdf", PAGES))

    response = client.get(f"/project/{project_id}/files/{record.id}?page=4")

    assert response.status_code == 404
    assert "page 4 of a 3-page ecf_17.pdf" in response.text


@pytest.mark.parametrize("query", ["?start=3", "?end=3", "?start=5&end=3"])
def test_a_range_missing_an_end_or_ending_before_it_starts_is_refused(
        project_id, tmp_path, query):
    record = _store(project_id, write_text_pdf(tmp_path / "ecf_17.pdf", PAGES))

    assert client.get(f"/project/{project_id}/files/{record.id}{query}").status_code == 422


def test_a_table_file_keeps_its_rows_and_shows_no_page(project_id):
    record = save_upload("posts.csv", io.BytesIO(b"a,b\n1,2\n"), project_id)

    page = _get(project_id, record)

    assert "source-page" not in page
    assert "1 rows × 2 columns" in page


def test_the_bytes_of_a_pdf_open_in_the_browser_and_of_anything_else_download(
        project_id, tmp_path):
    pdf = _store(project_id, write_text_pdf(tmp_path / "ecf_17.pdf", PAGES))
    html = tmp_path / "page.html"
    html.write_text("<p>stored</p>", encoding="utf-8")
    stored_html = _store(project_id, html)

    opened = client.get(f"/project/{project_id}/files/{pdf.id}/bytes")
    downloaded = client.get(f"/project/{project_id}/files/{stored_html.id}/bytes")

    assert opened.headers["content-type"] == "application/pdf"
    assert opened.headers["content-disposition"].startswith("inline")
    assert opened.content == (tmp_path / "ecf_17.pdf").read_bytes()
    assert downloaded.headers["content-disposition"].startswith("attachment")
