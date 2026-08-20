"""How a chat turn is DRAWN, case by case."""
from __future__ import annotations

import re

# Every bug this file exists for shipped because a test asserted that some markup was
# PRESENT rather than what shape the turn came out: a second file overwrote the first,
# the chips swallowed the words typed beside them, and the bubble stretched to the chip
# row's width. Each case below is a shape — how many chips, what prose, and which element
# holds what — so the next one fails here instead of on the page.

import pytest
from fastapi.testclient import TestClient

from app.core.agent.session import create_agent_session
from app.main import app
from app.services import workspace
from app.services.project import create_project
from app.web.file_sizes import read_turn

client = TestClient(app)

FILE_A = "[file] a.csv · 13B · in project demo (p1) · file id aaa"
FILE_B = "[file] b.parquet · 2.5MB · in project demo (p1) · file id bbb"


@pytest.fixture
def session_id(tmp_path, monkeypatch, scripted_agent_turn) -> str:
    workspace.set_projects_dir(tmp_path)
    # The turn must complete, or the page draws pending text, not the stored message.
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    create_project("demo", "A methodology.", source="test")
    return create_agent_session(
        "editing", {}, base_url="http://testserver/", title="t")


def render(session_id: str, text: str) -> str:
    """The turn as the page draws it, stored and re-read the way a reload would."""
    client.post(f"/chat/{session_id}/message", json={"text": text})
    page = client.get(f"/chat/{session_id}").text
    # From the user turn to whatever bubble follows it: matching the closing </div> by
    # regex would cut at the first nested one, which is where the chips live.
    start = page.index('<div class="ac-msg user">')
    rest = page[start + 1:]
    end = rest.find('<div class="ac-msg')
    return rest if end == -1 else rest[:end]


def chips(markup: str) -> list[str]:
    return re.findall(r'<span class="ac-file-name">([^<]*)</span>', markup)


def prose(markup: str) -> list[str]:
    return [body.strip() for body in
            re.findall(r'<div class="ac-body">(.*?)</div>', markup, re.S)]


# ─── The shapes ──────────────────────────────────────────────────────────────

def test_words_alone_are_one_bubble_and_no_chip(session_id):
    markup = render(session_id, "just a message")
    assert chips(markup) == []
    assert prose(markup) == ["just a message"]


def test_one_file_alone_is_one_chip_and_no_bubble(session_id):
    markup = render(session_id, FILE_A)
    assert chips(markup) == ["a.csv"]
    # No empty bubble under it: the file arriving IS the turn.
    assert prose(markup) == []


def test_a_file_with_words_keeps_both(session_id):
    markup = render(session_id, f"{FILE_A}\n\nrun this one")
    assert chips(markup) == ["a.csv"]
    assert prose(markup) == ["run this one"]


def test_every_file_gets_its_own_chip(session_id):
    markup = render(session_id, f"{FILE_A}\n{FILE_B}\n\nboth of these")
    assert chips(markup) == ["a.csv", "b.parquet"]
    assert prose(markup) == ["both of these"]


def test_the_chips_share_one_row_and_the_words_do_not(session_id):
    markup = render(session_id, f"{FILE_A}\n{FILE_B}\n\nboth of these")
    # The bubble is a SIBLING of the chips' row, not a flex item beside them — as an item
    # it stretched to the row's width instead of hugging its own words.
    assert markup.index('class="ac-files"') < markup.index('<div class="ac-body">')
    assert markup.count('class="ac-files"') == 1


def test_a_file_and_nothing_else_needs_no_row_for_words(session_id):
    assert '<div class="ac-body">' not in render(session_id, FILE_A)


# ─── The split the drawing is built on ───────────────────────────────────────

def test_the_size_rides_on_the_chip_and_the_rest_stays_in_the_text():
    turn = read_turn(f"{FILE_A}\n\nrun this one")
    assert [(f.name, f.meta) for f in turn.files] == [("a.csv", "13B")]
    assert turn.said == "run this one"
    # The project and the hash are in the TEXT, which is what the agent reads; on the
    # chip they would turn it into a paragraph.
    assert "p1" not in turn.files[0].meta and "aaa" not in turn.files[0].meta


def test_words_that_merely_mention_a_file_are_not_an_attachment():
    turn = read_turn("the [file] marker only counts at the start of a line")
    assert turn.files == []
    assert turn.said.startswith("the [file] marker")


def test_a_turn_with_no_words_says_so():
    assert read_turn(FILE_A).said == ""


def test_blank_lines_between_the_files_and_the_words_are_not_the_words():
    assert read_turn(f"{FILE_A}\n\n\n\nspaced out").said == "spaced out"
