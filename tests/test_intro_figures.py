from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from app.models.stages.llm_transform import LLMTransformStage
from app.services.project import WorkflowFile
from app.tools.tutorial import TUTORIAL_FIXTURE
from app.web.config import INTRO_DIR
from scripts.intro_figures import MoneyRow, compute_intro_figures

_BODY_ROW = re.compile(r"<tr>((?:<td[^>]*>[^<]*</td>)+)</tr>")
_CELL = re.compile(r"<td[^>]*>([^<]*)</td>")
_CARD_ENTRY = re.compile(r"<dt>([^<]*)</dt><dd>([^<]*)</dd>")
_NUMBER_TOKEN = re.compile(r"[\w.,-]*\d[\w.,-]*")


def test_every_number_on_the_intro_is_one_the_tour_fixture_computes():
    """The intro is static HTML, so this test is the only thing tying its figures to the fixture."""
    figures = compute_intro_figures()
    html = (INTRO_DIR / "index.html").read_text(encoding="utf-8")
    text = read_visible_text(html)

    filings = f"{figures.filings_read:,}"
    assert f"{filings} lobbying filings" in text

    waiting = f"{figures.paid_filings_for_review:,}"
    assert f"{waiting} paid filings" in text
    assert "pending" in text and figures.queue_is_open

    rows_shown = read_table_rows(html)
    rows_written = {format_money_row(row) for row in figures.money_rows}
    assert rows_shown and set(rows_shown) <= rows_written, set(rows_shown) - rows_written

    fixture = WorkflowFile.model_validate_json(TUTORIAL_FIXTURE.read_text(encoding="utf-8"))
    [ai_step] = [stage for stage in fixture.stages if isinstance(stage, LLMTransformStage)]
    card = dict(_CARD_ENTRY.findall(html))
    assert card == {
        "model": ai_step.llm.model,
        "temperature": str(ai_step.llm.temperature),
        "thinking": ai_step.llm.thinking,
    }

    sourced = {
        filings, waiting, *card.values(), *figures.export_names, *figures.years_read,
        *(cell.strip('"') for row in rows_shown for cell in row),
    }
    unsourced = find_number_tokens(text) - sourced
    assert not unsourced, f"/intro states numbers the tour fixture does not compute: {sorted(unsourced)}"


def read_visible_text(html: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(html)
    return "\n".join(parser.chunks)


def read_table_rows(html: str) -> list[tuple[str, ...]]:
    return [tuple(unescape(cell) for cell in _CELL.findall(row)) for row in _BODY_ROW.findall(html)]


def format_money_row(row: MoneyRow) -> tuple[str, ...]:
    return (
        row.client, quote_as_filed(row.income), quote_as_filed(row.expenses),
        str(row.income_usd), str(row.expenses_usd),
    )


def quote_as_filed(text: str | None) -> str:
    # The page shows a blank field as a dash and a filed one in quotes, as text.
    return f'"{text}"' if text else "—"


def find_number_tokens(text: str) -> set[str]:
    return {token.strip(".,-") for token in _NUMBER_TOKEN.findall(text)}


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.chunks.append(data)
