from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.frames import list_table_rows, table_from_rows
from app.models.locators import (
    CELL_KIND,
    LOCATOR_KINDS,
    PAGE_CHAR_RANGE_KIND,
    CellAt,
    CharRange,
    Locator,
    LocatorKindSpec,
    PageCharRange,
    label_locator,
    parse_locator,
    register_locator_kind,
)
from locator_kind_fixture import DOCKET_PAGE_KIND, DocketPage, registered_docket_page


@pytest.mark.parametrize("locator", [
    PageCharRange(page=3, start=10, end=42),
    CharRange(start=0, end=7),
    CellAt(row=0, column="amount"),
])
def test_a_locator_parses_back_as_its_own_class(locator: Locator) -> None:
    parsed = parse_locator(locator.model_dump())
    assert type(parsed) is type(locator)
    assert parsed == locator


@pytest.mark.parametrize(("range_class", "fields"), [
    (PageCharRange, {"page": 1, "start": 5, "end": 2}),
    (CharRange, {"start": 5, "end": 2}),
])
def test_a_range_that_ends_before_it_starts_is_refused(
    range_class: type[Locator], fields: dict[str, int],
) -> None:
    with pytest.raises(ValidationError, match="cannot end at 2, before it starts at 5"):
        range_class.model_validate(fields)


def test_a_locator_refuses_another_kinds_name() -> None:
    with pytest.raises(ValidationError, match="PageCharRange cannot hold kind 'cell'"):
        PageCharRange(kind=CELL_KIND, page=1, start=0, end=2)


def test_an_unregistered_kind_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match=r"no locator kind 'ecf_page' is registered.*page_char_range"):
        parse_locator({"kind": "ecf_page", "page": 14, "start": 0, "end": 5, "entry": 58})


def test_a_locator_naming_no_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="no locator kind None is registered"):
        parse_locator({"page": 14, "start": 0, "end": 5})


def test_a_kind_registers_once() -> None:
    with pytest.raises(ValueError, match="'page_char_range' is already registered"):
        register_locator_kind(LOCATOR_KINDS[PAGE_CHAR_RANGE_KIND])


def test_a_model_without_a_kind_default_cannot_register() -> None:
    with pytest.raises(ValueError, match="gives `kind` no default"):
        register_locator_kind(LocatorKindSpec(model=Locator, label=lambda locator: locator.kind))


def test_a_subclass_with_its_own_kind_parses_back_with_its_fields_and_label() -> None:
    with registered_docket_page():
        parsed = parse_locator({
            "kind": DOCKET_PAGE_KIND, "page": 14, "start": 0, "end": 5, "entry": 58,
        })
        assert parsed == DocketPage(page=14, start=0, end=5, entry=58)
        assert label_locator(parsed) == "ECF No. 58 at 14"


def test_nulls_a_struct_column_pads_in_from_other_kinds_are_dropped() -> None:
    written = [PageCharRange(page=2, start=0, end=4), CellAt(row=5, column="amount")]
    read_back = list_table_rows(table_from_rows([{"locator": w.model_dump()} for w in written]))
    assert read_back[0]["locator"]["row"] is None
    assert [parse_locator(row["locator"]) for row in read_back] == written


def test_a_value_the_kind_does_not_declare_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="page"):
        parse_locator({"kind": "char_range", "start": 0, "end": 4, "page": 2})
