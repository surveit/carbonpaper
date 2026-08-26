"""One published figure as a page for a reader outside the project: the claim, the
number, and what the run recorded behind it. Reads the scope map, adds nothing to it."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode

from pydantic import BaseModel

from app.core.errors import (
    DocumentNotFound,
    RowOutOfRange,
    RunVersionUnresolvableError,
    StageNotInRun,
)
from app.models.branch_analysis import BranchId
from app.models.citations import CitedValue
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.runtime.citations import build_row_trace_url, read_citations
from app.runtime.errors import MissingLineage
from app.services import run as run_service
from app.services.methodology import read_methodology
from app.services.project_record import read_project_name
from app.web import scope_view
from app.web.diagrams import TYPE_CLASS, TYPE_LABEL
from app.web.scope_payload import CutRows, ScopeMap

# The two TYPE_CLASS values that are not code running the same way every time.
JUDGED_BY_A_MODEL = "llm"
SIGNED_OFF_BY_A_PERSON = "human"


@dataclass(frozen=True)
class NoReceipt:
    """Why this run cannot say what is behind its own figure."""

    reason: str


class Step(BaseModel):
    """One stage the figure came through, as a reader of no workflow meets it."""

    stage_id: StageId
    type_label: str
    description: str


class StepsByHand(BaseModel):
    ran_as_code: list[Step]
    judged_by_a_model: list[Step]
    signed_off_by_a_person: list[Step]


class RowsTakenOut(BaseModel):
    at_stage: StageId
    rows: int
    why: str


class FigureReceipt(BaseModel):
    """Every count here is the run's own record of what it did, never a prediction."""

    counted_from_stage: StageId
    counted_from_rows: int
    read_from_stage: StageId
    read_from_rows: int
    taken_out: list[RowsTakenOut]
    unrecorded_rows: int
    steps: StepsByHand
    reference_tables: list[StageId]
    rows_href: str
    trace_href: str


Receipt = FigureReceipt | NoReceipt


@dataclass(frozen=True)
class MethodDocument:
    """The method's own title and opening, read off the document and not summarised."""

    title: str
    opening: str


@dataclass(frozen=True)
class FigureCard:
    project_id: str
    run_id: str
    label: str
    # The cell as the publish stage recorded it, unrounded and carrying no unit.
    value: str
    project_name: str
    document: MethodDocument | None
    counted_at: str
    version_id: str
    receipt: Receipt
    run_href: str


def load_figure_card(project_id: str, run_id: str,
                     citation: StageOutputCellCitation) -> FigureCard | None:
    """None where no publish stage in this run claimed that cell as a figure."""
    published = find_published_figure(project_id, run_id, citation)
    if published is None:
        return None
    manifest = run_service.read_run_status(project_id, run_id)
    return FigureCard(
        project_id=project_id, run_id=run_id,
        label=published.label, value=published.value,
        project_name=read_project_name(project_id),
        document=_read_the_documents_opening(project_id),
        counted_at=str(manifest.get("started_at") or ""),
        version_id=str(manifest.get("workflow_version") or ""),
        receipt=_read_the_receipt(project_id, run_id, citation),
        run_href=(f"/project/{quote(project_id, safe='')}"
                  f"/runs/{quote(run_id, safe='')}"),
    )


def find_published_figure(project_id: str, run_id: str,
                          citation: StageOutputCellCitation) -> CitedValue | None:
    """A figure IS a publish stage's claim about a cell; without one there is no claim."""
    return next((cited for cited in read_citations(project_id, run_id)
                 if cited.stage_id == citation.stage_id
                 and cited.row_ordinal == citation.row_ordinal
                 and cited.column == citation.column), None)


def describe_figure_for_a_link_preview(card: FigureCard) -> str:
    """What a pasted link unfurls as, counted off the receipt rather than written ahead."""
    counted = _describe_what_it_was_counted_from(card.receipt)
    return f"{card.label}: {card.value}. {counted} {_describe_the_method(card)}"


def _describe_what_it_was_counted_from(receipt: Receipt) -> str:
    if isinstance(receipt, NoReceipt):
        return "This run did not record which rows the figure came from."
    hands = _count_the_hands(receipt.steps)
    return (f"Counted from {receipt.counted_from_rows:,} rows of "
            f"{receipt.read_from_rows:,} read from source, over {hands}.")


def _count_the_hands(steps: StepsByHand) -> str:
    judged = len(steps.judged_by_a_model)
    signed = len(steps.signed_off_by_a_person)
    return (f"{len(steps.ran_as_code)} steps of code, "
            f"{judged} a model judged and {signed} a person signed off")


def _describe_the_method(card: FigureCard) -> str:
    if card.document is None:
        return f"From {card.project_name} in Carbon Paper."
    return f"From {card.document.title}, in Carbon Paper."


def _read_the_receipt(project_id: str, run_id: str,
                      citation: StageOutputCellCitation) -> Receipt:
    try:
        scope, cuts = scope_view.load_scope_map(project_id, run_id, citation)
    except (MissingLineage, StageNotInRun, RowOutOfRange, DocumentNotFound,
            FileNotFoundError, RunVersionUnresolvableError) as unrecorded:
        return NoReceipt(reason=str(unrecorded))
    widest = max(scope.scale, key=lambda step: step.rows_count)
    return FigureReceipt(
        counted_from_stage=scope.covers.at_stage,
        counted_from_rows=len(scope.covers.ordinals),
        read_from_stage=widest.stage, read_from_rows=widest.rows_count,
        taken_out=_read_what_was_taken_out(scope, cuts),
        unrecorded_rows=len(scope.covers.fed_by_no_rows),
        steps=group_steps_by_hand(scope),
        reference_tables=list(scope.lookup_tables),
        rows_href=_build_scope_url(project_id, run_id, citation),
        trace_href=build_row_trace_url(project_id, run_id, citation.stage_id,
                                       citation.row_ordinal, column=citation.column),
    )


def group_steps_by_hand(scope: ScopeMap) -> StepsByHand:
    """Deterministic code, a model's judgement and a person's decision are three claims."""
    by_class: dict[str, list[Step]] = {}
    for drawn in scope.stages:
        # Both maps are total over StageType; test_stage_type_presentation.py holds it.
        step = Step(stage_id=drawn.id, type_label=TYPE_LABEL[drawn.type],
                    description=drawn.description)
        by_class.setdefault(TYPE_CLASS[drawn.type], []).append(step)
    judged = by_class.pop(JUDGED_BY_A_MODEL, [])
    signed = by_class.pop(SIGNED_OFF_BY_A_PERSON, [])
    return StepsByHand(
        ran_as_code=[step for steps in by_class.values() for step in steps],
        judged_by_a_model=judged, signed_off_by_a_person=signed)


def _read_what_was_taken_out(scope: ScopeMap,
                             cuts: dict[BranchId, CutRows]) -> list[RowsTakenOut]:
    return [RowsTakenOut(at_stage=scope.branches[branch_id].stage_id,
                         rows=cut.total, why=scope.branches[branch_id].label)
            for branch_id, cut in cuts.items() if branch_id in scope.branches]


def _read_the_documents_opening(project_id: str) -> MethodDocument | None:
    text = read_methodology(project_id)
    if text is None:
        return None
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    if not blocks or not blocks[0].startswith("# "):
        return None
    return MethodDocument(title=blocks[0].removeprefix("# ").strip(),
                          opening=blocks[1] if len(blocks) > 1 else "")


def _build_scope_url(project_id: str, run_id: str,
                     citation: StageOutputCellCitation) -> str:
    asked = urlencode({"stage": citation.stage_id, "row": citation.row_ordinal,
                       "column": citation.column})
    return (f"/project/{quote(project_id, safe='')}"
            f"/runs/{quote(run_id, safe='')}/scope?{asked}")
