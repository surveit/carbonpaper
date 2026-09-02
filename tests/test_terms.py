"""app/models/terms.py + app/services/terms.py — a project's nouns and verbs, and the
block an agent is handed them in. Both halves are one stored document per project. What
is worth pinning: a noun that is only a word claims no kind, a word never means two
things, a project that stored nothing has nothing, and no words render as nothing."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models import NamedSchema, SchemaLibrary, Terms, Verb
from app.models.terms import render_terms
from app.services import terms
from app.models.records.terms import StoredTerms
from app.web.config import templates
from app.web.diagrams import SCHEMA_KIND_CLASS, SCHEMA_KIND_GLYPH
from app.services.methodology import write_methodology

_FLAG = Verb(
    name="flag",
    definition="Mark a row for a human to decide on.",
    also_written=["flagged", "flagging"],
)
_ISSUE_TEXT = NamedSchema(name="issue_text", title="Issue text")
_PROJECT = "vocab_project"


# ── the noun half: a word with no table ──────────────────────────────────────
def test_a_noun_that_is_only_a_word_carries_no_kind_and_no_columns():
    terms.write_terms(_PROJECT, Terms(nouns=SchemaLibrary(schemas=[_ISSUE_TEXT]), verbs=[]))

    noun = terms.load_terms(_PROJECT).nouns.schemas[0]
    assert noun.kind is None          # no kind stored is no source claimed
    assert noun.columns == []


def test_a_declared_kind_still_has_to_be_one_of_the_four():
    with pytest.raises(ValidationError):
        NamedSchema.model_validate({"name": "x", "title": "X", "kind": "vocabulary"})


# ── one word, one meaning ────────────────────────────────────────────────────
def test_a_word_that_is_both_a_noun_and_a_verb_is_refused():
    with pytest.raises(ValidationError, match="flag"):
        Terms(nouns=SchemaLibrary(schemas=[NamedSchema(name="flag", title="Flag")]), verbs=[_FLAG])


def test_a_verb_spelling_that_repeats_another_verbs_name_is_refused():
    flagged = Verb(name="flagged", definition="Already marked.")
    with pytest.raises(ValidationError, match="flagged"):
        Terms(nouns=SchemaLibrary(schemas=[]), verbs=[_FLAG, flagged])


def test_a_noun_spelling_that_repeats_a_verb_is_refused():
    # The pair the artifact exists for: the document says one word, the stages another.
    registrant = NamedSchema(name="registrant", title="Registrant", also_written=["flag"])
    with pytest.raises(ValidationError, match="flag"):
        Terms(nouns=SchemaLibrary(schemas=[registrant]), verbs=[_FLAG])


def test_two_nouns_written_the_same_second_way_are_refused():
    firm = NamedSchema(name="firm", title="Firm", also_written=["registrant"])
    filer = NamedSchema(name="filer", title="Filer", also_written=["registrant"])
    with pytest.raises(ValidationError, match="registrant"):
        Terms(nouns=SchemaLibrary(schemas=[firm, filer]), verbs=[])


def test_a_nouns_other_spellings_read_back_from_the_store():
    firm = NamedSchema(name="firm", title="Firm", also_written=["registrant", "filer"])
    terms.write_terms(_PROJECT, Terms(nouns=SchemaLibrary(schemas=[firm]), verbs=[]))

    library = terms.load_terms(_PROJECT).nouns
    assert library.schemas[0].also_written == ["registrant", "filer"]


def test_two_verbs_sharing_neither_a_name_nor_a_spelling_are_kept():
    resolve = Verb(name="resolve", definition="Settle a flagged row.")
    both = Terms(nouns=SchemaLibrary(schemas=[_ISSUE_TEXT]), verbs=[_FLAG, resolve])
    assert [verb.name for verb in both.verbs] == ["flag", "resolve"]


# ── storage ──────────────────────────────────────────────────────────────────
def test_a_project_that_stored_nothing_has_no_words():
    stored = terms.load_terms(_PROJECT)
    assert stored.nouns.schemas == []
    assert stored.verbs == []


def test_both_halves_read_back_from_the_one_stored_document():
    terms.write_terms(_PROJECT, Terms(nouns=SchemaLibrary(schemas=[_ISSUE_TEXT]), verbs=[_FLAG]))

    stored = terms.load_terms(_PROJECT)
    assert [schema.name for schema in stored.nouns.schemas] == ["issue_text"]
    assert stored.verbs == [_FLAG]


def test_writing_no_verbs_retires_the_ones_already_stored():
    terms.write_terms(_PROJECT, Terms(nouns=SchemaLibrary(schemas=[]), verbs=[_FLAG]))
    terms.write_terms(_PROJECT, Terms(nouns=SchemaLibrary(schemas=[]), verbs=[]))
    assert terms.load_terms(_PROJECT).verbs == []


def test_generated_nouns_keep_the_verbs_the_project_already_agreed():
    terms.write_terms(_PROJECT, Terms(nouns=SchemaLibrary(schemas=[]), verbs=[_FLAG]))
    terms.write_nouns(_PROJECT, SchemaLibrary(schemas=[_ISSUE_TEXT]))

    stored = terms.load_terms(_PROJECT)
    assert [schema.name for schema in stored.nouns.schemas] == ["issue_text"]
    assert stored.verbs == [_FLAG]


def test_a_stored_document_whose_two_halves_share_a_word_is_refused():
    # Written past write_terms, which only ever takes an already-composed Terms.
    StoredTerms(
        id=f"{_PROJECT}/terms",
        nouns=SchemaLibrary(schemas=[NamedSchema(name="flag", title="Flag")]),
        verbs=[_FLAG],
    ).save()

    with pytest.raises(ValidationError, match="flag"):
        terms.load_terms(_PROJECT)


def test_one_projects_terms_are_not_read_under_a_project_whose_id_it_extends():
    terms.write_terms("venezuela_lobbying", Terms(nouns=SchemaLibrary(schemas=[]), verbs=[_FLAG]))
    assert terms.load_terms("venezuela").verbs == []


# ── nouns authored before the store ──────────────────────────────────────────
def _write_schema_file(projects_root, schema: NamedSchema) -> None:
    schemas_dir = projects_root / _PROJECT / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    (schemas_dir / f"01_{schema.name}.json").write_text(
        json.dumps(schema.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )


def test_schema_files_written_before_the_store_are_still_read(projects_root):
    _write_schema_file(projects_root, _ISSUE_TEXT)

    stored = terms.load_terms(_PROJECT)
    assert [schema.name for schema in stored.nouns.schemas] == ["issue_text"]
    assert stored.verbs == []


def test_the_first_write_moves_a_project_into_the_store_for_good(projects_root):
    _write_schema_file(projects_root, _ISSUE_TEXT)
    terms.write_terms(_PROJECT, Terms(nouns=SchemaLibrary(schemas=[]), verbs=[_FLAG]))

    # The file is still there and is no longer what the project says.
    assert (projects_root / _PROJECT / "schemas" / "01_issue_text.json").is_file()
    assert terms.load_terms(_PROJECT).nouns.schemas == []


# ── the block an agent is handed ─────────────────────────────────────────────
def test_a_project_with_no_words_renders_nothing_at_all():
    assert render_terms(Terms(nouns=SchemaLibrary(schemas=[]), verbs=[])) == ""


def test_a_project_with_only_verbs_renders_no_noun_heading():
    block = render_terms(Terms(nouns=SchemaLibrary(schemas=[]), verbs=[_FLAG]))
    assert "Nouns:" not in block
    assert "- flag — Mark a row for a human to decide on." in block


def test_the_block_carries_every_word_its_meaning_and_its_other_spellings():
    firm = NamedSchema(
        name="firm",
        title="Firm",
        description="A company that filed.",
        also_written=["registrant"],
    )
    block = render_terms(Terms(nouns=SchemaLibrary(schemas=[firm]), verbs=[_FLAG]))

    assert "- firm — A company that filed. Also written: registrant." in block
    assert "- flag — Mark a row for a human to decide on. Also written: flagged, flagging." in block
    assert "synonym" in block  # the framing: do not introduce one


def test_a_noun_that_is_only_a_word_is_rendered_under_its_title():
    block = render_terms(Terms(nouns=SchemaLibrary(schemas=[_ISSUE_TEXT]), verbs=[]))
    assert "- issue_text — Issue text" in block


# ── the Glossary tab, rendered over a word with a table and a word without ──
def _render_terms_section(stored: Terms | None, unreadable: str = "") -> str:
    template = templates.env.get_template("section_methodology.html")
    context = template.new_context({
        "state": {"id": _PROJECT},
        # Non-empty, so the Methodology tab's own empty-state never fires — these
        # tests are about the Glossary tab only.
        "methodology": "Stub methodology text.",
        "active_tab": "glossary",
        "terms": stored,
        "unreadable": unreadable,
        "kind_class": SCHEMA_KIND_CLASS,
        "kind_glyph": SCHEMA_KIND_GLYPH,
    })
    return "".join(template.blocks["section"](context))


def test_the_section_shows_a_noun_with_no_columns_without_marking_it_short_of_any():
    html = _render_terms_section(
        Terms(nouns=SchemaLibrary(schemas=[_ISSUE_TEXT]), verbs=[])
    )
    assert "issue_text" in html          # never dropped for having no table
    assert "0 column" not in html        # a count would read as data missing
    assert "Columns" not in html         # nor a reference section over nothing


def test_the_section_shows_a_nouns_columns_and_the_spellings_of_both_halves():
    firm = NamedSchema(
        name="firm",
        title="Firm",
        kind="input",
        also_written=["registrant"],
        columns=[{"name": "firm_id", "type": "str", "nullable": False}],
    )
    html = _render_terms_section(Terms(nouns=SchemaLibrary(schemas=[firm]), verbs=[_FLAG]))

    assert "1 column" in html
    assert "firm_id" in html                      # the column table, not just the count
    assert "input" in html                        # the kind it declared
    assert html.count("also written: registrant") == 1
    assert html.count("also written: flagged") == 1
    assert "Mark a row for a human to decide on." in html


def test_the_section_tells_a_project_with_no_words_what_to_do():
    html = _render_terms_section(Terms(nouns=SchemaLibrary(schemas=[]), verbs=[]))
    assert "empty-state" in html
    assert "assistant" in html      # who agrees them with you
    assert "dm-card" not in html


def test_the_section_says_why_terms_it_could_not_read_are_not_shown():
    html = _render_terms_section(None, unreadable="word(s) carrying more than one meaning: ['flag']")
    assert "flag" in html
    assert "empty-state" not in html   # unreadable is not the same as unagreed


# ── the route, over a project with words and one without ────────────────────
def _get_terms_page(tmp_path, stored: Terms | None):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import workspace

    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / "vocab"
    project_dir.mkdir()
    write_methodology((project_dir).name, "Follow the filings.")
    if stored is not None:
        terms.write_terms("vocab", stored)
    return TestClient(app).get("/project/vocab/methodology?tab=glossary")


def test_the_route_renders_both_halves_of_what_the_project_stored(tmp_path):
    firm = NamedSchema(
        name="firm", title="Firm", description="A company that filed.",
        columns=[{"name": "firm_id", "type": "str", "nullable": False}],
    )
    response = _get_terms_page(
        tmp_path, Terms(nouns=SchemaLibrary(schemas=[firm]), verbs=[_FLAG])
    )

    assert response.status_code == 200
    assert "A company that filed." in response.text            # the noun
    assert "Mark a row for a human to decide on." in response.text   # the verb
    assert "firm_id" in response.text                          # the noun's columns
    assert 'href="/project/vocab/methodology"' in response.text   # its own nav entry


def test_the_route_renders_a_project_that_has_agreed_no_words(tmp_path):
    response = _get_terms_page(tmp_path, None)

    assert response.status_code == 200
    assert "No terms agreed yet" in response.text
