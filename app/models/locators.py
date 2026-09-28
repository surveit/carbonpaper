"""Where a quote sits in a stored Source: one Locator subclass per kind, registered in LOCATOR_KINDS."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Generic, Self, TypeVar

from pydantic import Field, model_validator

from app.models.base import _Base

PAGE_CHAR_RANGE_KIND = "page_char_range"
CHAR_RANGE_KIND = "char_range"
CELL_KIND = "cell"


# Each subclass names its kind as `kind`'s default, including one that extends another kind.
class Locator(_Base):
    kind: str

    @model_validator(mode="after")
    def _kind_is_the_class_default(self) -> Self:
        if self.kind != type(self).model_fields["kind"].default:
            raise ValueError(f"{type(self).__name__} cannot hold kind {self.kind!r}")
        return self

    def validate_text(self, text: str) -> None:
        """A kind that can tell its page by its text overrides this to raise QuoteNotAtAddress."""


# Offsets here and in CharRange are slice bounds: `text[start:end]` is the quote.
class PageCharRange(Locator):
    kind: str = PAGE_CHAR_RANGE_KIND
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> Self:
        _refuse_end_before_start(self.start, self.end)
        return self


class CharRange(Locator):
    kind: str = CHAR_RANGE_KIND
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> Self:
        _refuse_end_before_start(self.start, self.end)
        return self


class CellAt(Locator):
    """A tabular Source's cell. `row` counts from 0, as a run numbers its rows."""
    kind: str = CELL_KIND
    row: int = Field(ge=0)
    column: str


KindLocator = TypeVar("KindLocator", bound=Locator)


class LocatorKindSpec(_Base, Generic[KindLocator]):
    model: type[KindLocator]
    label: Callable[[KindLocator], str]


LOCATOR_KINDS: dict[str, LocatorKindSpec[Any]] = {}


def register_locator_kind(spec: LocatorKindSpec[Any]) -> None:
    kind = spec.model.model_fields["kind"].default
    if not isinstance(kind, str):
        raise ValueError(f"{spec.model.__name__} gives `kind` no default, so it names no kind")
    if kind in LOCATOR_KINDS:
        raise ValueError(f"locator kind {kind!r} is already registered")
    LOCATOR_KINDS[kind] = spec


def parse_locator(fields: Mapping[str, object]) -> Locator:
    """A struct column pads each locator with other kinds' fields as nulls; those are dropped."""
    model = _find_kind_spec(fields.get("kind")).model
    own_fields = {
        name: value for name, value in fields.items()
        if name in model.model_fields or value is not None
    }
    return model.model_validate(own_fields, strict=True)


def parse_any_locator(value: object) -> object:
    """A locator or its fields, as its registered kind; any other value is left for pydantic to refuse."""
    fields = value.model_dump() if isinstance(value, Locator) else value
    return parse_locator(fields) if isinstance(fields, Mapping) else fields


def label_locator(locator: Locator) -> str:
    return _find_kind_spec(locator.kind).label(locator)


def _find_kind_spec(kind: object) -> LocatorKindSpec[Any]:
    if not isinstance(kind, str) or kind not in LOCATOR_KINDS:
        raise ValueError(
            f"no locator kind {kind!r} is registered; registered kinds: {sorted(LOCATOR_KINDS)}"
        )
    return LOCATOR_KINDS[kind]


def _refuse_end_before_start(start: int, end: int) -> None:
    if end < start:
        raise ValueError(f"a range cannot end at {end}, before it starts at {start}")


def _label_page_char_range(locator: PageCharRange) -> str:
    return f"page {locator.page}"


def _label_char_range(locator: CharRange) -> str:
    return f"characters {locator.start}–{locator.end}"


def _label_cell(locator: CellAt) -> str:
    return f"row {locator.row + 1}, column {locator.column}"


register_locator_kind(LocatorKindSpec(model=PageCharRange, label=_label_page_char_range))
register_locator_kind(LocatorKindSpec(model=CharRange, label=_label_char_range))
register_locator_kind(LocatorKindSpec(model=CellAt, label=_label_cell))
