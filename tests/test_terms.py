"""app/models/terms.py + app/services/terms.py — a project's words and how agents read them."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models import NamedSchema, RowType, SchemaLibrary, Terms, Verb
from app.models.terms import render_terms
from app.services import terms
from app.models.records.terms import StoredTerms
from app.web.config import templates
from app.web.diagrams import SCHEMA_KIND_CLASS, SCHEMA_KIND_GLYPH
from app.services.methodology import write_methodology

_FLAG = Verb(name="flag", definition="Mark a row for a human to decide on.")
_ISSUE = RowType(id="issue", title="Issue", definition="One thing a reader should look at.")
_ISSUE_TEXT = NamedSchema(name="issue_text", title="Issue text", row_type_id="issue")
_UNTYPED_TABLE = NamedSchema(name="issue_text", title="Issue text")
_NO_SCHEMAS = SchemaLibrary(schemas=[])
_PROJECT = "vocab_project"


# ── a table names the row type its rows are ──────────────────────────────────
def test_a_schema_names_the_row_type_one_of_its_rows_is():
    terms.write_terms(_PROJECT, Terms(
        row_types=[_ISSUE], schemas=SchemaLibrary(schemas=[_ISSUE_TEXT]), verbs=[]))

    assert terms.load_terms(_PROJECT).schemas.schemas[0].row_type_id == "issue"


def test_many_schemas_may_name_the_same_row_type():
    kept = NamedSchema(name="issue_kept", title="Issue kept", row_type_id="issue")
    stored = Terms(
        row_types=[_ISSUE], schemas=SchemaLibrary(schemas=[_ISSUE_TEXT, kept]), verbs=[])
    assert [schema.row_type_id for schema in stored.schemas.schemas] == ["issue", "issue"]


def test_a_schema_naming_no_row_type_is_kept():
    stored = Terms(row_types=[], schemas=SchemaLibrary(schemas=[_UNTYPED_TABLE]), verbs=[])
    assert stored.schemas.schemas[0].row_type_id is None


def test_a_row_type_id_naming_a_row_type_nobody_declared_is_refused():
    ghost_table = NamedSchema(name="issue_text", title="Issue text", row_type_id="ghost")
    with pytest.raises(ValidationError, match="ghost"):
        Terms(row_types=[], schemas=SchemaLibrary(schemas=[ghost_table]), verbs=[])


def test_that_refusal_names_the_schema_that_pointed_at_it():
    ghost_table = NamedSchema(name="issue_text", title="Issue text", row_type_id="ghost")
    with pytest.raises(ValidationError, match="issue_text"):
        Terms(row_types=[], schemas=SchemaLibrary(schemas=[ghost_table]), verbs=[])


def test_a_row_types_id_is_snake_case_like_a_schema_name():
    with pytest.raises(ValidationError):
        RowType(id="BadName", title="Bad", definition="A row.")


def test_a_declared_kind_still_has_to_be_one_of_the_four():
    with pytest.raises(ValidationError):
        NamedSchema.model_validate({"name": "x", "title": "X", "kind": "vocabulary"})


def test_a_schema_no_longer_carries_its_own_spellings():
    with pytest.raises(ValidationError):
        NamedSchema.model_validate({"name": "x", "title": "X", "also_written": ["ex"]})


# ── one word, one meaning ────────────────────────────────────────────────────
def test_a_word_that_is_both_a_row_type_and_a_verb_is_refused():
    flag = RowType(id="flag", title="Flag", definition="A row marked for a human.")
    with pytest.raises(ValidationError, match="flag"):
        Terms(row_types=[flag], schemas=_NO_SCHEMAS, verbs=[_FLAG])


def test_two_verbs_of_one_name_are_refused():
    twin = Verb(name="flag", definition="Already marked.")
    with pytest.raises(ValidationError, match="flag"):
        Terms(row_types=[], schemas=_NO_SCHEMAS, verbs=[_FLAG, twin])


def test_two_row_types_of_one_id_are_refused():
    firm = RowType(id="firm", title="Firm", definition="A company.")
    twin = RowType(id="firm", title="Filer", definition="A filer.")
    with pytest.raises(ValidationError, match="firm"):
        Terms(row_types=[firm, twin], schemas=_NO_SCHEMAS, verbs=[])


def test_a_schema_named_after_a_verb_is_kept():
    # A schema name addresses a table; only row types and verbs say a word.
    schemas = SchemaLibrary(schemas=[NamedSchema(name="flag", title="Flag")])
    both = Terms(row_types=[], schemas=schemas, verbs=[_FLAG])
    assert [schema.name for schema in both.schemas.schemas] == ["flag"]


def test_two_verbs_of_different_names_are_kept():
    resolve = Verb(name="resolve", definition="Settle a flagged row.")
    both = Terms(row_types=[_ISSUE], schemas=_NO_SCHEMAS, verbs=[_FLAG, resolve])
    assert [verb.name for verb in both.verbs] == ["flag", "resolve"]


# ── storage ──────────────────────────────────────────────────────────────────
def test_a_project_that_stored_nothing_has_no_words():
    stored = terms.load_terms(_PROJECT)
    assert stored.row_types == []
    assert stored.schemas.schemas == []
    assert stored.verbs == []


def test_every_part_reads_back_from_the_one_stored_document():
    terms.write_terms(_PROJECT, Terms(
        row_types=[_ISSUE], schemas=SchemaLibrary(schemas=[_ISSUE_TEXT]), verbs=[_FLAG]))

    stored = terms.load_terms(_PROJECT)
    assert [row_type.id for row_type in stored.row_types] == ["issue"]
    assert [schema.name for schema in stored.schemas.schemas] == ["issue_text"]
    assert stored.verbs == [_FLAG]


def test_writing_no_verbs_retires_the_ones_already_stored():
    terms.write_terms(_PROJECT, Terms(row_types=[], schemas=_NO_SCHEMAS, verbs=[_FLAG]))
    terms.write_terms(_PROJECT, Terms(row_types=[], schemas=_NO_SCHEMAS, verbs=[]))
    assert terms.load_terms(_PROJECT).verbs == []


def test_a_stored_document_whose_words_repeat_is_refused():
    # Written past write_terms, which only ever takes an already-composed Terms.
    StoredTerms(
        id=f"{_PROJECT}/terms",
        row_types=[RowType(id="flag", title="Flag", definition="A row marked for a human.")],
        schemas=_NO_SCHEMAS,
        verbs=[_FLAG],
    ).save()

    with pytest.raises(ValidationError, match="flag"):
        terms.load_terms(_PROJECT)


def test_a_stored_schema_pointing_at_no_declared_row_type_is_refused():
    StoredTerms(
        id=f"{_PROJECT}/terms",
        row_types=[],
        schemas=SchemaLibrary(schemas=[
            NamedSchema(name="issue_text", title="Issue text", row_type_id="ghost")
        ]),
        verbs=[],
    ).save()

    with pytest.raises(ValidationError, match="ghost"):
        terms.load_terms(_PROJECT)


def test_one_projects_terms_are_not_read_under_a_project_whose_id_it_extends():
    terms.write_terms(
        "venezuela_lobbying", Terms(row_types=[], schemas=_NO_SCHEMAS, verbs=[_FLAG]))
    assert terms.load_terms("venezuela").verbs == []


# ── schemas authored before the store ────────────────────────────────────────
def _write_schema_file(projects_root, schema: NamedSchema) -> None:
    schemas_dir = projects_root / _PROJECT / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    (schemas_dir / f"01_{schema.name}.json").write_text(
        json.dumps(schema.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )


def test_schema_files_written_before_the_store_are_still_read(projects_root):
    _write_schema_file(projects_root, _UNTYPED_TABLE)

    stored = terms.load_terms(_PROJECT)
    assert [schema.name for schema in stored.schemas.schemas] == ["issue_text"]
    assert stored.verbs == []


def test_the_first_write_moves_a_project_into_the_store_for_good(projects_root):
    _write_schema_file(projects_root, _UNTYPED_TABLE)
    terms.write_terms(_PROJECT, Terms(row_types=[], schemas=_NO_SCHEMAS, verbs=[_FLAG]))

    # The file is still there and is no longer what the project says.
    assert (projects_root / _PROJECT / "schemas" / "01_issue_text.json").is_file()
    assert terms.load_terms(_PROJECT).schemas.schemas == []


# ── the block an agent is handed ─────────────────────────────────────────────
def test_a_project_with_no_words_renders_nothing_at_all():
    assert render_terms(Terms(row_types=[], schemas=_NO_SCHEMAS, verbs=[])) == ""


def test_a_project_whose_only_words_are_verbs_renders_no_row_type_heading():
    block = render_terms(Terms(row_types=[], schemas=_NO_SCHEMAS, verbs=[_FLAG]))
    assert "Row types:" not in block
    assert "- flag — Mark a row for a human to decide on." in block


def test_the_block_names_the_words_and_nothing_about_the_tables():
    block = render_terms(Terms(
        row_types=[_ISSUE], schemas=SchemaLibrary(schemas=[_ISSUE_TEXT]), verbs=[_FLAG]))

    assert "- issue — One thing a reader should look at." in block
    assert "- flag — Mark a row for a human to decide on." in block
    assert "issue_text" not in block
    assert "Schemas" not in block and "Tables" not in block


def test_a_project_whose_only_entries_are_tables_renders_nothing_at_all():
    assert render_terms(
        Terms(row_types=[], schemas=SchemaLibrary(schemas=[_UNTYPED_TABLE]), verbs=[])) == ""


def test_the_block_carries_every_word_and_its_meaning():
    firm = RowType(id="firm", title="Firm", definition="A company that filed.")
    block = render_terms(Terms(row_types=[firm], schemas=_NO_SCHEMAS, verbs=[_FLAG]))

    assert "- firm — A company that filed." in block
    assert "- flag — Mark a row for a human to decide on." in block
    assert "synonym" in block  # the framing: do not introduce one


# ── the Glossary tab, rendered over a word with a table and a word without ──
def _render_terms_section(stored: Terms | None, unreadable: str = "") -> str:
    template = templates.env.get_template("section_methodology.html")
    context = template.new_context({
        "state": {"id": _PROJECT},
        # Non-empty, so it never shows the Methodology tab's own empty-state.
        "methodology": "Stub methodology text.",
        "active_tab": "glossary",
        "terms": stored,
        "unreadable": unreadable,
        "kind_class": SCHEMA_KIND_CLASS,
        "kind_glyph": SCHEMA_KIND_GLYPH,
    })
    return "".join(template.blocks["section"](context))


def test_the_section_shows_a_schema_with_no_columns_without_marking_it_short_of_any():
    html = _render_terms_section(
        Terms(row_types=[], schemas=SchemaLibrary(schemas=[_UNTYPED_TABLE]), verbs=[])
    )
    assert "issue_text" in html          # never dropped for having no table
    assert "0 column" not in html        # a count would read as data missing
    assert "Columns" not in html         # nor a reference section over nothing


def test_the_section_shows_a_row_type_its_table_and_a_verb():
    firm_word = RowType(id="firm", title="Firm", definition="A company that filed.")
    firm_table = NamedSchema(
        name="firm_filings",
        title="Firm filings",
        kind="input",
        row_type_id="firm",
        columns=[{"name": "firm_id", "type": "str", "nullable": False}],
    )
    html = _render_terms_section(Terms(
        row_types=[firm_word], schemas=SchemaLibrary(schemas=[firm_table]), verbs=[_FLAG]))

    assert "1 column" in html
    assert "firm_id" in html                      # the column table, not just the count
    assert "input" in html                        # the kind it declared
    assert "A company that filed." in html        # the row type's definition
    assert "Mark a row for a human to decide on." in html


def test_the_section_tells_a_project_with_no_words_what_to_do():
    html = _render_terms_section(Terms(row_types=[], schemas=_NO_SCHEMAS, verbs=[]))
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


def test_the_route_renders_every_part_of_what_the_project_stored(tmp_path):
    firm_word = RowType(id="firm", title="Firm", definition="A company that filed.")
    firm_table = NamedSchema(
        name="firm_filings", title="Firm filings", row_type_id="firm",
        columns=[{"name": "firm_id", "type": "str", "nullable": False}],
    )
    response = _get_terms_page(tmp_path, Terms(
        row_types=[firm_word], schemas=SchemaLibrary(schemas=[firm_table]), verbs=[_FLAG]))

    assert response.status_code == 200
    assert "A company that filed." in response.text                  # the row type
    assert "Mark a row for a human to decide on." in response.text   # the verb
    assert "firm_id" in response.text                                # the table's columns
    assert 'href="/project/vocab/methodology"' in response.text      # its own nav entry


def test_the_route_renders_a_project_that_has_agreed_no_words(tmp_path):
    response = _get_terms_page(tmp_path, None)

    assert response.status_code == 200
    assert "No terms agreed yet" in response.text
