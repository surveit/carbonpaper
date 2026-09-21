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
    name_the_columns_tested,
    name_the_columns_written,
    name_the_group_keys,
    name_the_looked_up_columns,
    name_the_reference_input,
    read_the_predicate,
    reviews_every_row,
    say_the_line_lower,
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
    # The stage's own authored line, for a reader who wants it beside the clause.
    description: str = ""


class SupportedStatement(BaseModel):
    """A paragraph per grain: a regrain closes the one it appears in."""

    paragraphs: list[list[FigureClause]] = Field(default_factory=list)


# Said in the hover of any noun this run's own version did not name.
LATER_VERSION_NOTE = (
    "Named by the project's workflow as it stands now; this run's version named nothing.")


def build_supported_statement(
    steps: Sequence[FigureStep], cited_column: str, nouns_are_later: bool = False
) -> SupportedStatement:
    """`steps` is the lineage walk, upstream first, the stage the figure is cited on last."""
    paragraphs: list[list[FigureClause]] = [[]]
    for position, step in enumerate(steps):
        kind = _read_clause_kind(step, is_the_figure=position == len(steps) - 1)
        if kind is None:
            continue
        place = _place_the_step(steps, position, cited_column, paragraphs[-1],
                                nouns_are_later)
        paragraphs[-1].append(FigureClause(
            kind=kind, stage_id=step.stage.stage.id,
            phrases=_TELLERS[kind](step, place),
            description=step.stage.stage.description))
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
    # Whether the nouns come from a version later than the one that ran.
    nouns_are_later: bool


def _place_the_step(
    steps: Sequence[FigureStep], position: int, cited_column: str,
    paragraph: list[FigureClause], nouns_are_later: bool,
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
                  cited_column=cited_column, nouns_are_later=nouns_are_later)


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
                         hover=_hover_the_population(step, place, noun))]
    # Two loads meet at a union, and a reader needs to know which rows came from where.
    if place.loads > 1:
        said += [text_phrase(" from "),
                 name_phrase(step.stage.stage.id, hover=step.stage.stage.description)]
    said.append(text_phrase("."))
    if place.loads_at < place.loads - 1:
        return said
    return said + _say_what_the_figure_reads(step)


def _hover_the_population(step: FigureStep, place: _Place, noun: str) -> str:
    behind = (f"{say_count(step.rows.rows_behind)} of them behind this figure"
              if step.rows.rows_behind else "none of them behind this figure")
    definition = f"{step.row_type.definition} " if step.row_type else ""
    return (f"{definition}{say_count(step.rows.rows_out)} {noun} here, {behind}."
            + _say_where_the_noun_came_from(step, place))


def _say_where_the_noun_came_from(step: FigureStep, place: _Place) -> str:
    return (f" {LATER_VERSION_NOTE}"
            if place.nouns_are_later and step.row_type is not None else "")


def _say_what_the_figure_reads(step: FigureStep) -> list[Phrase]:
    """What the VALUE came through. What each decision read is said at the decision."""
    columns = [_say_a_column(step, name) for name in step.rows.columns_behind]
    if not columns:
        return []
    return ([text_phrase(" The figure's value comes through ")] + say_list(columns)
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
    if predicate is None:
        return _say_the_step_s_own_test(step, place)
    held = [Phrase(text=f"that {predicate}", hover=_hover_the_restriction(step, place))]
    if not step.rows.rows_dropped:
        return ([text_phrase("Every one ")] + held
                + [text_phrase(", so the step narrowed nothing.")])
    if place.narrows_first:
        return ([text_phrase(f"Of those {say_plural(place.incoming)}, only the ones ")]
                + held + [text_phrase(" go on.")])
    return [text_phrase("Of those, the ones ")] + held + [text_phrase(" remain.")]


def _say_the_step_s_own_test(step: FigureStep, place: _Place) -> list[Phrase]:
    """Nobody wrote what the test means for this sentence, so the step's own line is it."""
    named = name_phrase(step.stage.stage.id, hover=_hover_the_restriction(step, place))
    said = say_the_line_lower(step.stage.stage.description).rstrip(".")
    opening = (f"Of those {say_plural(place.incoming)}, " if place.narrows_first
               else "Of those, ")
    if not step.rows.rows_dropped:
        return [text_phrase(f"{opening}none was dropped: "), named,
                text_phrase(f" {said}.")]
    return [text_phrase(opening), named, text_phrase(f" {said}.")]


def _hover_the_restriction(step: FigureStep, place: _Place) -> str:
    tested = name_the_columns_tested(step.stage)
    reads = f" · tested against {', '.join(tested)}" if tested else ""
    return (f"{say_count(step.rows.rows_out)} of {say_count(place.rows_in)} go on"
            f" · {say_share(step.rows.rows_out, place.rows_in)}{reads}")


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
    """The grain moved here, so the clause says outright what one row is from now on."""
    asserts = says_nothing_was_combined(step.stage)
    return (_open_the_regrain(step, asserts) + [text_phrase(". ")]
            + _declare_the_grain(step, place))


def _declare_the_grain(step: FigureStep, place: _Place) -> list[Phrase]:
    keys = name_the_group_keys(step.stage)
    named = _say_the_new_noun(step, place)
    if named:
        return [text_phrase("From here, one row is one ")] + named + [text_phrase(".")]
    # No word for it, so the keys say what one row is: a pair of them is not "one iso3".
    said = say_and_list([_say_a_column(step, name) for name in keys])
    closing = "." if len(keys) < 2 else " pair."
    return [text_phrase("From here, one row is one ")] + said + [text_phrase(closing)]


def _open_the_regrain(step: FigureStep, asserts: bool) -> list[Phrase]:
    per_key = say_and_list([_say_a_column(step, name)
                            for name in name_the_group_keys(step.stage)])
    if asserts:
        return ([text_phrase("The resulting table holds one row per ")] + per_key
                + [text_phrase(", "), Phrase(text="the duplicates having to agree",
                                             hover=_hover_the_assert(step))])
    return ([text_phrase("The resulting table gathers them into one row per ")] + per_key
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
    hover = (f"{step.row_type.definition} {say_count(place.rows_in)} "
             f"{say_plural(place.incoming)} become {say_count(step.rows.rows_out)}."
             + _say_where_the_noun_came_from(step, place))
    return [count_phrase(step.row_type.title.lower(), hover=hover)]


# ── a step that wrote on every row ──────────────────────────────────────────

def _say_the_transform(step: FigureStep, place: _Place) -> list[Phrase]:
    adds, rewrites = name_the_columns_written(step.stage)
    hand = ("A model" if StageType(step.stage.stage.type) is StageType.llm_transform
            else "Code")
    verb = "gives" if adds else "rewrites"
    written = _say_the_written(step, adds or rewrites)
    return ([text_phrase(f"{hand} "),
             Phrase(text=f"{verb} each", hover=_hover_the_transform(step, place)),
             text_phrase(" ")] + written + [text_phrase(".")])


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
