"""The shared schema-table macro is what a reviewer reads to decide whether they
believe a stage. A constraint the runtime enforces but the table omits means the
reviewer signs off on a weaker claim than the pipeline actually makes."""

from app.models.schema import Column, TableSchema
from app.web.config import templates

MACRO = (
    '{% from "_schema_table.html" import schema_table %}{{ schema_table(schema) }}'
)


def _render(*columns: Column) -> str:
    schema = TableSchema(columns=list(columns))
    return templates.env.from_string(MACRO).render(schema=schema)


def test_an_enum_column_shows_its_vocabulary_not_an_em_dash():
    html = _render(
        Column(
            name="spend_band",
            type="str",
            nullable=False,
            enum=["high", "low"],
            description="high at 100,000 US dollars or more, else low.",
        )
    )

    assert "high · low" in html
    assert "—" not in html


def test_a_numeric_range_column_still_shows_its_bounds():
    html = _render(Column(name="spend_rank", type="int", nullable=False, range=[1, "inf"]))

    assert "1" in html and "inf" in html
    assert "—" not in html


def test_a_column_constrained_by_neither_shows_the_em_dash():
    html = _render(Column(name="client", type="str", nullable=False))

    assert "—" in html
