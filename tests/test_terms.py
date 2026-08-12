"""app/models/terms.py + app/services/terms.py — a project's nouns and verbs.
Both halves are one stored document per project. What is worth pinning: a noun that is
only a word claims no kind, a word never means two things, a project that stored nothing
has nothing, and the schema files projects were authored with before the store still read."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models import NamedSchema, SchemaLibrary, Terms, Verb
from app.services import terms
from app.services.terms import StoredTerms
from app.web.config import templates
from app.web.diagrams import SCHEMA_KIND_CLASS, SCHEMA_KIND_GLYPH, SCHEMA_KIND_ORDER

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


# ── the data-model section, rendered over both kinded and kindless schemas ───
def _render_data_model_section(schemas: list[dict[str, object]]) -> str:
    template = templates.env.get_template("section_data_model.html")
    context = template.new_context({
        "state": {"id": _PROJECT},
        "schemas": schemas,
        "issues": [],
        "er_diagram": None,
        "table_graph": None,
        "schema_json": {},
        "kind_order": SCHEMA_KIND_ORDER,
        "kind_class": SCHEMA_KIND_CLASS,
        "kind_glyph": SCHEMA_KIND_GLYPH,
    })
    return "".join(template.blocks["section"](context))


def test_the_section_groups_a_schema_that_declares_no_kind_as_itself():
    html = _render_data_model_section([
        {"name": "filing", "kind": "input", "title": "Filing", "columns": []},
        {"name": "issue_text", "title": "Issue text", "columns": []},
    ])
    assert "issue_text" in html            # never dropped for having no kind
    assert "no kind" in html
    assert html.count("type-tag") == 3     # two group chips, one kinded schema's summary chip
