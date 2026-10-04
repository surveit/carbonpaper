"""A queued row's span and list[span] values read as their quotes, each linked to its page."""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.files import list_project_files
from app.main import app
from app.models.connectors import SOURCE_COLUMNS
from app.services import run as run_service
from conftest import queue_added_columns, queue_columns, reads_of
from pdf_fixture import write_text_pdf
from stage_seed import save_version, set_stages
from test_claim_review_span_citations import (  # noqa: F401  (the fixture comes with them)
    _KIND,
    END,
    PAGE,
    QUOTE,
    START,
    letter_pack,
)

PROJECT = "span_queue"
_PAGE_SPAN = {"name": "page_span", "type": "span", "nullable": False}
_SAID = {"name": "said", "type": "span", "nullable": False}
_BOTH = {"name": "both", "type": "list[span]", "nullable": False}
_PAGE = {"name": "page", "type": "int", "nullable": False}

_QUOTES_THE_PAGE = f'''
from app.models.spans import Span, narrow_span

def transform(row):
    said = narrow_span(Span.model_validate(row["page_span"]), {QUOTE!r}).model_dump()
    return dict(row, said=said, both=[said, row["page_span"]])
'''


@pytest.fixture
def queue_page(projects_root: Path, letter_pack: None) -> str:  # noqa: F811
    folder = projects_root / PROJECT / "letters"
    folder.mkdir(parents=True)
    write_text_pdf(folder / "letter.pdf", [PAGE])
    set_stages(PROJECT, _stage_specs(folder))
    save_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])
    with TestClient(app) as client:
        response = client.get(f"/project/{PROJECT}/runs/{run_id}/queue/review")
    assert response.status_code == 200, response.text
    return response.text


def test_a_queued_span_reads_as_its_quote_then_its_page_of_its_file(queue_page):
    [letter] = list_project_files(PROJECT)
    said_page = f"/project/{PROJECT}/files/{letter.id}?page=1&start={START}&end={END}"

    said_cite = f'<q title="{QUOTE}">{QUOTE}</q> <a href="{escape(said_page)}">p. 1 of letter.pdf</a>'
    # Once as `said` and once first in `both`.
    assert queue_page.count(said_cite) == 2
    assert f'<q title="{PAGE}">{PAGE}</q>' in queue_page
    assert "source_sha256" not in queue_page


def _stage_specs(folder: Path) -> list[dict[str, Any]]:
    naming_a_file = [column.model_dump(mode="json", exclude_defaults=True)
                     for column in SOURCE_COLUMNS[:2]]
    return [
        {"id": "letters", "description": "Read the letters", "type": "input_data",
         "connector": {"kind": _KIND, "params": {"folder": str(folder)}},
         "signature": {"form": "replaces", "produces": [
             column.model_dump(mode="json", exclude_defaults=True)
             for column in SOURCE_COLUMNS]}},
        {"id": "pages", "description": "Read each letter a page at a time",
         "type": "read_pages", "inputs": [{"id": "letters"}], "row_type_id": "letter_page",
         "read_pages": {"carry": []},
         "signature": {"form": "replaces", "reads": reads_of("letters", naming_a_file),
                       "produces": [*naming_a_file, _PAGE,
                                    {"name": "page_text", "type": "str", "nullable": False},
                                    _PAGE_SPAN]}},
        {"id": "quoted", "description": "Quotes what the letter says",
         "type": "python_row_function", "inputs": [{"id": "pages"}],
         "function": {"kind": "inline", "code": _QUOTES_THE_PAGE},
         "signature": {"form": "extends", "reads": reads_of("pages", [_PAGE_SPAN]),
                       "adds": [_SAID, _BOTH]}},
        {"id": "review", "description": "Review the quotes", "type": "human_review_queue",
         "inputs": [{"id": "quoted"}],
         "signature": {"form": "extends",
                       "reads": reads_of("quoted", [_PAGE, _SAID, _BOTH]),
                       "adds": queue_added_columns()},
         "queue": queue_columns(source="page")},
    ]
