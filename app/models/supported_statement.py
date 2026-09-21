"""A figure's route through a run, told as prose: one clause per stage that changed it."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.models.row_types import NO_KIND_ROW_TYPE_ID, RowType
from app.models.schema import StageId
from app.models.stages.stage_base import StageType
from app.models.supported_phrases import (
    Phrase,
    column_phrase,
    count_phrase,
    name_phrase,
    say_and_list,
    say_count,
    say_list,
    say_plural,
    say_share,
    text_phrase,
)
from app.models.supported_verbs import (
    find_the_formula_behind,
    list_the_formulas,
    name_the_columns_written,
    name_the_group_keys,
    name_the_looked_up_columns,
    name_the_reference_input,
    read_the_predicate,
    reviews_every_row,
    say_the_formulas,
    says_nothing_was_combined,
    writes_columns,
)
from app.models.workflow_stage import WorkflowStage

_RESTRICTIONS = (StageType.filter_rows, StageType.starlark_filter_rows)
_LOOKUPS = (StageType.enrich, StageType.expand)


class ClauseKind(str, Enum):
    population = "population"
    restriction = "restriction"
    review = "review"
    lookup = "lookup"
    regrain = "regrain"
    transform = "transform"
    head = "head"


@dataclass(frozen=True)
class StepRows:
    """One stage's rows: what it wrote, what it took out, and what this figure rests on."""

    rows_out: int
    rows_dropped: int
    rows_behind: int
    columns_behind: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FigureStep:
    """One stage of the walk, with its counts and the word for one of its rows."""

    stage: WorkflowStage
    rows: StepRows
    # None where nothing in the stage's ancestry named a row type: the clause says "rows".
    row_type: Optional[RowType] = None


class FigureClause(BaseModel):
    kind: ClauseKind
    stage_id: StageId
    phrases: list[Phrase]
    # The stage's own authored line, which the list view prints under every clause.
    description: str = ""
    # Set where the clause's own words say no more than what the step is called.
    needs_the_description: bool = False


class SupportedStatement(BaseModel):
    """A paragraph per grain: a regrain closes the one it appears in."""

    paragraphs: list[list[FigureClause]] = Field(default_factory=list)


def build_supported_statement(
    steps: Sequence[FigureStep], cited_column: str
) -> SupportedStatement:
    """`steps` is the lineage walk, upstream first, the stage the figure is cited on last."""
    paragraphs: list[list[FigureClause]] = [[]]
    for position, step in enumerate(steps):
        kind = _read_clause_kind(step, is_the_figure=position == len(steps) - 1)
        if kind is None:
            continue
        place = _place_the_step(steps, position, cited_column, paragraphs[-1])
        paragraphs[-1].append(FigureClause(
            kind=kind, stage_id=step.stage.stage.id,
            phrases=_TELLERS[kind](step, place),
            description=step.stage.stage.description,
            needs_the_description=_says_only_a_name(kind, step)))
        if kind is ClauseKind.regrain:
            paragraphs.append([])
    return SupportedStatement(paragraphs=[held for held in paragraphs if held])


@dataclass(frozen=True)
class _Place:
    """Where a clause sits: what reached it, and whether it opens its paragraph."""

    rows_in: int
    incoming: Optional[RowType]
    # Which load of the walk's loads this is: two populations can meet at a union.
    loads_at: int
    loads: int
    # Whether this is the paragraph's first narrowing, which is the one that names the noun.
    narrows_first: bool
    cited_column: str


def _place_the_step(
    steps: Sequence[FigureStep], position: int, cited_column: str,
    paragraph: list[FigureClause],
) -> _Place:
    before = steps[position - 1] if position else None
    # What the step before wrote: a regrain's own rows_in counts nothing it collapsed.
    rows_in = before.rows.rows_out if before else steps[position].rows.rows_out
    loads = [at for at, held in enumerate(steps) if _loads_rows(held)]
    return _Place(loads_at=loads.index(position) if position in loads else 0,
                  loads=len(loads),
                  rows_in=rows_in,
                  incoming=before.row_type if before else None,
                  narrows_first=not any(clause.kind is ClauseKind.restriction
                                        for clause in paragraph),
                  cited_column=cited_column)


def _loads_rows(step: FigureStep) -> bool:
    return StageType(step.stage.stage.type) is StageType.input_data


def _read_clause_kind(step: FigureStep, is_the_figure: bool) -> Optional[ClauseKind]:
    stage_type = StageType(step.stage.stage.type)
    if is_the_figure:
        return ClauseKind.head
    if stage_type is StageType.input_data:
        return ClauseKind.population
    if stage_type in _RESTRICTIONS:
        return ClauseKind.restriction
    if stage_type is StageType.human_review_queue:
        return ClauseKind.review
    if stage_type in _LOOKUPS:
        return ClauseKind.lookup
    if _regrains(step):
        return ClauseKind.regrain
    if writes_columns(step.stage):
        return ClauseKind.transform
    # A stage that changed neither the rows, their columns nor their word says nothing.
    return None


def _says_only_a_name(kind: ClauseKind, step: FigureStep) -> bool:
    """A test nobody wrote a predicate for is named, never explained, by this builder."""
    return (kind in (ClauseKind.restriction, ClauseKind.review)
            and read_the_predicate(step.stage) is None)


def _regrains(step: FigureStep) -> bool:
    stage_type = StageType(step.stage.stage.type)
    if stage_type is StageType.dedupe:
        return True
    return stage_type is StageType.aggregate and bool(name_the_group_keys(step.stage))


# ── the population ───────────────────────────────────────────────────────────

def _say_the_population(step: FigureStep, place: _Place) -> list[Phrase]:
    noun = say_plural(step.row_type)
    said = [text_phrase("The run loads " if not place.loads_at else "It also loads "),
            count_phrase(f"{say_count(step.rows.rows_out)} {noun}",
                         hover=_hover_the_population(step, noun))]
    # Two loads meet at a union, and a reader needs to know which rows came from where.
    if place.loads > 1:
        said += [text_phrase(" from "),
                 name_phrase(step.stage.stage.id, hover=step.stage.stage.description)]
    said.append(text_phrase("."))
    if place.loads_at < place.loads - 1:
        return said
    return said + _say_what_the_figure_reads(step)


def _hover_the_population(step: FigureStep, noun: str) -> str:
    behind = (f"{say_count(step.rows.rows_behind)} of them behind this figure"
              if step.rows.rows_behind else "none of them behind this figure")
    definition = f"{step.row_type.definition} " if step.row_type else ""
    return f"{definition}{say_count(step.rows.rows_out)} {noun} here, {behind}."


def _say_what_the_figure_reads(step: FigureStep) -> list[Phrase]:
    columns = [_say_a_column(step, name) for name in step.rows.columns_behind]
    if not columns:
        return []
    return ([text_phrase(" The figure reads ")] + say_list(columns)
            + [text_phrase(".")])


def _say_a_column(step: FigureStep, name: str) -> Phrase:
    return column_phrase(name, _describe_column(step, name))


def _describe_column(step: FigureStep, name: str) -> Optional[str]:
    """Its own frame first, then what reached it: an aggregation reads its input's column."""
    schemas = [step.stage.output_schema] + [held.table_schema for held in step.stage.inputs]
    for schema in schemas:
        column = schema.column_for_name(name) if schema else None
        if column is not None and column.description:
            return column.description
    return None


# ── a step that narrowed the population ──────────────────────────────────────

def _say_the_restriction(step: FigureStep, place: _Place) -> list[Phrase]:
    predicate = read_the_predicate(step.stage)
    held = _hold_the_test(step, place, predicate)
    if not step.rows.rows_dropped:
        return _say_the_narrowing_that_was_not(held, predicate)
    if place.narrows_first:
        return ([text_phrase(f"Of those {say_plural(place.incoming)}, only the ones ")]
                + held + [text_phrase(" go on.")])
    return [text_phrase("Of those, the ones ")] + held + [text_phrase(" remain.")]


def _say_the_narrowing_that_was_not(
    held: list[Phrase], predicate: Optional[str]
) -> list[Phrase]:
    if predicate is None:
        return ([text_phrase("Every one went on, so ")]
                + held[:1] + [text_phrase(" narrowed nothing.")])
    return ([text_phrase("Every one ")] + held
            + [text_phrase(", so the step narrowed nothing.")])


def _hold_the_test(
    step: FigureStep, place: _Place, predicate: Optional[str]
) -> list[Phrase]:
    hover = _hover_the_restriction(step, place)
    # Nobody wrote what the test means, so the step's own name is all there is to say.
    if predicate is None:
        return [name_phrase(step.stage.stage.id, hover=hover), text_phrase(" kept")]
    return [Phrase(text=f"that {predicate}", hover=hover)]


def _hover_the_restriction(step: FigureStep, place: _Place) -> str:
    return (f"{say_count(step.rows.rows_out)} of {say_count(place.rows_in)} go on"
            f" · {say_share(step.rows.rows_out, place.rows_in)}")


# ── a step a person or a lookup stood in ─────────────────────────────────────

def _say_the_review(step: FigureStep, place: _Place) -> list[Phrase]:
    predicate = read_the_predicate(step.stage) or "went in front of a person"
    if not reviews_every_row(step.stage):
        return _say_the_partial_review(step, predicate)
    hover = f"All {say_count(step.rows.rows_out)} of them were reviewed at this step."
    return [text_phrase("Each "), Phrase(text=predicate, hover=hover),
            text_phrase(" before it counted.")]


def _say_the_partial_review(step: FigureStep, predicate: str) -> list[Phrase]:
    """A queue with a filter or a routing rule reviewed some rows, and the run records which."""
    hover = f"{say_count(step.rows.rows_out)} rows reached this step; the queue selected from them."
    return [text_phrase("The ones "), name_phrase(step.stage.stage.id, hover=hover),
            text_phrase(f" selected {predicate} before it counted.")]


def _say_the_lookup(step: FigureStep, place: _Place) -> list[Phrase]:
    landed = name_the_looked_up_columns(step.stage)
    reference = name_the_reference_input(step.stage)
    hover = (f"{say_count(step.rows.rows_out)} rows carried on; a lookup that matched "
             f"nothing is not counted here.")
    if not landed:
        return [text_phrase("Each is matched against "),
                _say_the_reference(step, reference, hover), text_phrase(".")]
    return ([text_phrase("Each has ")]
            + say_and_list([_say_a_column(step, name) for name in landed])
            + [text_phrase(" "), Phrase(text="looked up", hover=hover),
               text_phrase(".")])


def _say_the_reference(step: FigureStep, reference: Optional[str], hover: str) -> Phrase:
    if reference is None:
        return Phrase(text="its reference table", hover=hover)
    return name_phrase(reference, hover=hover)


# ── a step that changed what one row is ──────────────────────────────────────

def _say_the_regrain(step: FigureStep, place: _Place) -> list[Phrase]:
    asserts = says_nothing_was_combined(step.stage)
    noun = _say_the_new_noun(step, place)
    if not noun:
        return _open_the_regrain(step, asserts) + [text_phrase(".")]
    joined = ", and becomes " if asserts else ", becoming "
    return _open_the_regrain(step, asserts) + [text_phrase(joined)] + noun + [
        text_phrase(".")]


def _open_the_regrain(step: FigureStep, asserts: bool) -> list[Phrase]:
    per_key = say_and_list([_say_a_column(step, name)
                            for name in name_the_group_keys(step.stage)])
    if asserts:
        return ([text_phrase("What survives is held to one row per ")] + per_key
                + [text_phrase(", "), Phrase(text="the duplicates having to agree",
                                             hover=_hover_the_assert(step))])
    return ([text_phrase("These are gathered into one row per ")] + per_key
            + [text_phrase(", their values "),
               Phrase(text=say_the_formulas(step.stage),
                      hover=_hover_the_gather(step))])


def _hover_the_assert(step: FigureStep) -> str:
    counted = Counter(list_the_formulas(step.stage))
    if not counted:
        return "Rows agreeing on the keys collapse to one, or the run stops."
    return (f"Every field held this way must agree across the duplicates, or the run "
            f"stops: {counted['only']} held that way, {counted['list']} listed instead.")


def _hover_the_gather(step: FigureStep) -> str:
    keys = name_the_group_keys(step.stage)
    said = keys[0] if len(keys) == 1 else f"{', '.join(keys[:-1])} and {keys[-1]}"
    return f"Values are combined across the rows sharing {said}."


def _say_the_new_noun(step: FigureStep, place: _Place) -> list[Phrase]:
    if step.row_type is None or step.row_type.id == NO_KIND_ROW_TYPE_ID:
        return []
    if place.incoming is not None and step.row_type.id == place.incoming.id:
        return []
    hover = (f"{say_count(place.rows_in)} {say_plural(place.incoming)} become "
             f"{say_count(step.rows.rows_out)}.")
    return [count_phrase(say_plural(step.row_type), hover=hover)]


# ── a step that wrote on every row ──────────────────────────────────────────

def _say_the_transform(step: FigureStep, place: _Place) -> list[Phrase]:
    adds, rewrites = name_the_columns_written(step.stage)
    hover = _hover_the_transform(step, place)
    if StageType(step.stage.stage.type) is StageType.llm_transform:
        lead = [text_phrase("A model read each and "),
                Phrase(text="gave it" if adds else "rewrote", hover=hover),
                text_phrase(" ")]
        return lead + _say_the_written(step, adds or rewrites) + [text_phrase(".")]
    if not adds:
        return ([text_phrase("Each has ")] + _say_the_written(step, rewrites)
                + [Phrase(text=" rewritten", hover=hover), text_phrase(".")])
    return ([text_phrase("Each is "), Phrase(text="given", hover=hover),
             text_phrase(" ")] + _say_the_written(step, adds) + [text_phrase(".")])


def _say_the_written(step: FigureStep, columns: list[str]) -> list[Phrase]:
    return say_and_list([_say_a_column(step, name) for name in columns])


def _hover_the_transform(step: FigureStep, place: _Place) -> str:
    if not step.rows.rows_dropped:
        return f"All {say_count(place.rows_in)} rows carried on."
    return (f"{say_count(step.rows.rows_out)} of {say_count(place.rows_in)} carried on"
            f" · {say_share(step.rows.rows_out, place.rows_in)}")


# ── the figure itself ────────────────────────────────────────────────────────

def _say_the_head(step: FigureStep, place: _Place) -> list[Phrase]:
    formula = find_the_formula_behind(step.stage, place.cited_column)
    if formula is None:
        return _say_the_figure_plainly(step, place)
    keys = name_the_group_keys(step.stage)
    source, said = formula
    if keys:
        return _say_the_grouped_head(step, place, source, said, keys)
    return _say_the_scalar_head(step, place, source, said)


def _say_the_scalar_head(
    step: FigureStep, place: _Place, source: Optional[str], said: str
) -> list[Phrase]:
    hover = f"Over {say_count(place.rows_in)} {say_plural(place.incoming)}."
    if source is None:
        return [text_phrase("Those rows, "), Phrase(text=said, hover=hover),
                text_phrase(", are the figure.")]
    return ([text_phrase("Their "), _say_a_column(step, source), text_phrase(", "),
             Phrase(text=said, hover=hover), text_phrase(", is the figure.")])


def _say_the_grouped_head(
    step: FigureStep, place: _Place, source: Optional[str], said: str, keys: list[str]
) -> list[Phrase]:
    per_key = say_and_list([_say_a_column(step, name) for name in keys])
    opening = ([text_phrase("Their "), _say_a_column(step, source)]
               if source is not None else [text_phrase("The rows")])
    return (opening + [text_phrase(f" is {said} for each ")] + per_key
            + [text_phrase(" — ")] + _say_the_head_count(step)
            + [text_phrase(" in all — and that is the figure.")])


def _say_the_head_count(step: FigureStep) -> list[Phrase]:
    rows_out = step.rows.rows_out
    return [count_phrase(f"{say_count(rows_out)} {say_plural(step.row_type)}",
                         hover=f"{say_count(rows_out)} rows out.")]


def _say_the_figure_plainly(step: FigureStep, place: _Place) -> list[Phrase]:
    hover = f"{say_count(step.rows.rows_out)} rows out."
    return [text_phrase("Their "), _say_a_column(step, place.cited_column),
            text_phrase(", as "), name_phrase(step.stage.stage.id, hover=hover),
            text_phrase(" wrote it, is the figure.")]


_TELLERS = {
    ClauseKind.population: _say_the_population,
    ClauseKind.restriction: _say_the_restriction,
    ClauseKind.review: _say_the_review,
    ClauseKind.lookup: _say_the_lookup,
    ClauseKind.regrain: _say_the_regrain,
    ClauseKind.transform: _say_the_transform,
    ClauseKind.head: _say_the_head,
}
