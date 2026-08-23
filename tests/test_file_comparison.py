"""Which column tells one file from another of its shape."""
from __future__ import annotations

from app.core.column_profile import ValueCount
from app.core.file_comparison import (
    ColumnDisagreement, choose_the_telling_column, group_files_by_columns,
    rank_columns_by_disagreement,
)
from app.core.file_shape import ColumnKind, ColumnShape, FileShape

# The four exports carry the same queries; what differs is how much of each they hold.
QUERIES = ["socialType:facebook AND postType:re", "Comments on climate posts",
           "260810 all activist pages"]


def column(name, values, *, rows, kind=ColumnKind.CATEGORY, timeline=()):
    filled = sum(count for _, count in values)
    return ColumnShape(
        column=name, kind=kind, null_count=rows - filled, blank_count=0,
        filled_count=filled, distinct_count=len(values),
        top=[ValueCount(value=value, count=count) for value, count in values],
        timeline=[ValueCount(value=day, count=count) for day, count in timeline],
    )


def shape(*columns, rows):
    return FileShape(row_count=rows, columns=list(columns))


def mixture(shares, rows=1000, name="Input Name"):
    return column(name, [(QUERIES[i], int(rows * share)) for i, share in enumerate(shares)],
                  rows=rows)


def test_files_sharing_a_column_set_are_one_group():
    shapes = {"a": shape(mixture([0.5, 0.5, 0.0]), rows=1000),
              "b": shape(mixture([0.2, 0.8, 0.0]), rows=1000),
              "c": shape(column("term", [("merde", 60)], rows=60), rows=60)}
    groups = group_files_by_columns(shapes)
    assert [group.file_ids for group in groups] == [["a", "b"], ["c"]]


def test_one_file_has_nothing_to_differ_from():
    assert rank_columns_by_disagreement([shape(mixture([1.0, 0.0, 0.0]), rows=10)]) == []


def test_files_agreeing_on_every_column_separate_by_nothing():
    same = [shape(mixture([0.4, 0.4, 0.2]), rows=1000) for _ in range(2)]
    assert rank_columns_by_disagreement(same) == []


def test_the_same_values_in_different_proportions_still_separate_them():
    # The trap a value-overlap score falls into: identical vocabularies, different mix.
    ranked = rank_columns_by_disagreement([shape(mixture([0.9, 0.1, 0.0]), rows=1000),
                                           shape(mixture([0.1, 0.9, 0.0]), rows=1000)])
    assert [seen.column for seen in ranked] == ["Input Name"]
    assert ranked[0].score == 0.8


def test_a_column_of_ids_is_not_compared():
    # Its listed values cover 8 rows of 1000, so two files disagreeing says nothing.
    ids = [column("Document ID", [(f"170{i}", 1) for i in range(8)], rows=1000)]
    assert rank_columns_by_disagreement([shape(*ids, rows=1000),
                                         shape(*ids, rows=1000)]) == []


def test_a_date_column_is_left_to_its_span():
    dates = column("Date", [("2026-07-20", 500), ("2026-07-21", 500)], rows=1000,
                   kind=ColumnKind.DATE,
                   timeline=[("2026-07-20", 500), ("2026-07-21", 500)])
    other = column("Date", [("2026-08-01", 900), ("2026-08-02", 100)], rows=1000,
                   kind=ColumnKind.DATE,
                   timeline=[("2026-08-01", 900), ("2026-08-02", 100)])
    assert rank_columns_by_disagreement([shape(dates, rows=1000),
                                         shape(other, rows=1000)]) == []


def test_the_most_disagreed_about_column_leads():
    ranked = rank_columns_by_disagreement([
        shape(mixture([0.9, 0.1, 0.0]), mixture([0.5, 0.5, 0.0], name="Sentiment"), rows=1000),
        shape(mixture([0.1, 0.9, 0.0]), mixture([0.8, 0.2, 0.0], name="Sentiment"), rows=1000),
    ])
    assert [seen.column for seen in ranked] == ["Input Name", "Sentiment"]
    assert isinstance(ranked[0], ColumnDisagreement)


def test_a_column_the_files_nearly_agree_on_is_not_worth_naming():
    # 55/45 against 50/50 is a difference, and saying it would be noise.
    ranked = rank_columns_by_disagreement([shape(mixture([0.5, 0.5, 0.0]), rows=1000),
                                           shape(mixture([0.55, 0.45, 0.0]), rows=1000)])
    assert ranked == []


def test_the_column_read_is_the_one_whose_leading_values_differ():
    # Author Name disagrees more; both files lead with the same value in it.
    author = "Author Name"
    files = [
        shape(column(author, [("Valeurs actuelles", 550), ("CNEWS", 450)], rows=1000),
              column("source_file", [("climate.parquet", 600), ("activists.parquet", 400)],
                     rows=1000), rows=1000),
        shape(column(author, [("Valeurs actuelles", 950), ("CNEWS", 50)], rows=1000),
              column("source_file", [("activists.parquet", 600), ("climate.parquet", 400)],
                     rows=1000), rows=1000),
    ]
    ranked = rank_columns_by_disagreement(files)
    assert ranked[0].column == author                                # disagrees most
    assert choose_the_telling_column(files).column == "source_file"  # but says nothing


def test_nothing_to_choose_when_no_column_disagrees():
    same = [shape(mixture([0.4, 0.4, 0.2]), rows=1000) for _ in range(2)]
    assert choose_the_telling_column(same) is None
