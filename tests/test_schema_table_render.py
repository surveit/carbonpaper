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


POLICY_AREAS = [
    "Health", "Energy & Environment", "Finance & Taxation", "Technology",
    "Defense", "Transportation", "Agriculture", "Other",
]


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

    assert '<code class="enum-val">high</code>' in html
    assert '<code class="enum-val">low</code>' in html
    assert "—" not in html


def test_each_enum_value_is_its_own_chip_so_a_multi_word_value_reads_as_one():
    """Joined into one string, a multi-word value wraps mid-value and blurs into its neighbour."""
    html = _render(Column(name="policy_area", type="str", nullable=False, enum=POLICY_AREAS))

    assert '<code class="enum-val">Energy &amp; Environment</code>' in html
    assert " · " not in html


def test_a_numeric_range_column_still_shows_its_bounds():
    html = _render(Column(name="spend_rank", type="int", nullable=False, range=[1, "inf"]))

    assert "1" in html and "inf" in html
    assert "—" not in html


def test_a_column_constrained_by_neither_shows_the_em_dash():
    html = _render(Column(name="client", type="str", nullable=False))

    assert "—" in html


# ─── The reader is an editor, so the table says optional, never null ─────────


def test_the_table_says_required_and_optional_not_null():
    html = _render(
        Column(name="filing_id", type="str", nullable=False),
        Column(name="primary_ask", type="str", nullable=True),
    )

    assert ">required<" in html and ">optional<" in html
    assert "null" not in html.lower()


# ─── A long vocabulary folds, and says how much it is holding back ───────────


def test_a_long_vocabulary_folds_behind_a_count_that_names_every_value():
    html = _render(Column(name="policy_area", type="str", nullable=False, enum=POLICY_AREAS))

    assert "<details" in html
    assert ">8 values<" in html
    # Folded is not truncated: the closed cell already carries the whole vocabulary,
    # so expanding is a CSS reveal and nothing has to be fetched or reconstructed.
    for value in POLICY_AREAS:
        assert value.replace("&", "&amp;") in html


def test_a_short_vocabulary_stays_inline_with_no_fold_to_click():
    html = _render(Column(name="verdict", type="str", nullable=False, enum=["approve", "modify"]))

    assert "<details" not in html
    assert "values<" not in html
