"""app/models/terms.py + app/services/terms.py — a project's nouns and verbs.
The noun half is the named schemas under schemas/; the verb half is verbs.json beside
them. What is worth pinning: a noun that is only a word claims no kind, a word never
means two things, and a project that never wrote verbs.json simply has none."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models import NamedSchema, SchemaLibrary, Terms, Verb
from app.services import terms
from app.web.config import templates
from app.web.diagrams import SCHEMA_KIND_CLASS, SCHEMA_KIND_GLYPH, SCHEMA_KIND_ORDER

_FLAG = Verb(
    name="flag",
    definition="Mark a row for a human to decide on.",
    also_written=["flagged", "flagging"],
)
_ISSUE_TEXT = NamedSchema(name="issue_text", title="Issue text")


# ── the noun half: a word with no table ──────────────────────────────────────
def test_a_noun_that_is_only_a_word_carries_no_kind_and_no_columns(tmp_path):
    terms.write_data_model(tmp_path, SchemaLibrary(schemas=[_ISSUE_TEXT]))

    stored = json.loads((tmp_path / "schemas" / "01_issue_text.json").read_text(encoding="utf-8"))
    assert "kind" not in stored          # no kind written is no source claimed

    library = terms.load_data_model(tmp_path)
    assert library is not None
    assert library.schemas[0].kind is None
    assert library.schemas[0].columns == []


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
def test_a_project_with_no_verbs_file_has_no_verbs(tmp_path):
    assert not (tmp_path / "verbs.json").exists()
    assert terms.load_verbs(tmp_path) == []
    assert terms.load_terms(tmp_path).verbs == []


def test_verbs_round_trip_through_verbs_json(tmp_path):
    terms.write_verbs(tmp_path, [_FLAG])
    assert terms.load_verbs(tmp_path) == [_FLAG]


def test_writing_no_verbs_leaves_no_file(tmp_path):
    terms.write_verbs(tmp_path, [_FLAG])
    terms.write_verbs(tmp_path, [])
    assert not (tmp_path / "verbs.json").exists()


def test_load_terms_reads_both_halves_of_what_the_project_stored(tmp_path):
    terms.write_data_model(tmp_path, SchemaLibrary(schemas=[_ISSUE_TEXT]))
    terms.write_verbs(tmp_path, [_FLAG])

    stored = terms.load_terms(tmp_path)
    assert [schema.name for schema in stored.nouns.schemas] == ["issue_text"]
    assert stored.verbs == [_FLAG]


def test_load_terms_refuses_a_project_whose_two_halves_share_a_word(tmp_path):
    terms.write_data_model(tmp_path, SchemaLibrary(schemas=[NamedSchema(name="flag", title="Flag")]))
    terms.write_verbs(tmp_path, [_FLAG])

    with pytest.raises(ValidationError, match="flag"):
        terms.load_terms(tmp_path)


def test_a_verbs_file_that_is_not_a_list_of_verbs_fails_loudly(tmp_path):
    (tmp_path / "verbs.json").write_text('{"flag": "mark a row"}', encoding="utf-8")
    with pytest.raises(ValidationError):
        terms.load_verbs(tmp_path)


# ── the data-model section, rendered over both kinded and kindless schemas ───
def _render_data_model_section(schemas: list[dict[str, object]]) -> str:
    template = templates.env.get_template("section_data_model.html")
    context = template.new_context({
        "state": {"id": "vocab_project"},
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
