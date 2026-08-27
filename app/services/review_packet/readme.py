"""The packet's README: the finding, its sources and its steps as markdown, so the
folder can be pushed to a repository and read with no server and no viewer."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.core.ids import ID
from app.models.stages.stage_base import StageType
from app.services.review_packet.checksums import CHECKSUMS_FILE
from app.services.review_packet.data import (
    ARTIFACTS_DIR,
    DATA_DIR,
    DOCUMENT_FILE,
    EVENTS_FILE,
    INPUTS_DIR,
    MANIFEST_FILE,
    RAW_DIR,
    WORKFLOW_FILE,
)
from app.services.review_packet.packet_contents import (
    PacketClaim,
    PacketContents,
    PacketFlag,
    PacketSource,
    PacketStep,
    find_data_file,
    read_packet_contents,
)

README_FILE = "README.md"

# GitHub stops rendering a README somewhere past 512 KB, so a table that grows with the
# data is capped and the file holding all of it is linked instead.
MAX_TABLE_ROWS = 200
MAX_FLAG_CHARS = 240
MAX_NAMED_PARENTS = 3


def write_packet_readme(root: Path) -> str:
    contents = read_packet_contents(root)
    (root / README_FILE).write_text(_render_readme(contents), encoding="utf-8")
    return README_FILE


def _render_readme(contents: PacketContents) -> str:
    lines = [
        *_render_opening(contents),
        *_render_findings(contents),
        *_render_sources(contents),
        *_render_production(contents),
        *_render_flags(contents),
        *_render_steps(contents),
        *_render_folder(contents),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_opening(contents: PacketContents) -> list[str]:
    lines = [f"# {contents.title}", ""]
    if contents.opening:
        lines += [contents.opening, ""]
    return lines


def _render_findings(contents: PacketContents) -> list[str]:
    if not contents.claims:
        return []
    lines = ["## What this run found", ""]
    for claim in [c for c in contents.claims if c.primary]:
        source = _link_stage(contents.root, claim.stage_id)
        lines += [
            f"### {_render_cell(claim.value)}",
            "",
            f"{claim.label} — the first row's `{claim.column}` in {source}.",
            "",
        ]
    rest = [c for c in contents.claims if not c.primary]
    if rest:
        lines += _render_table(
            ["Figure", "Value", "Read from"],
            [_render_claim_row(contents.root, claim) for claim in rest],
            f"the rest are in [`{WORKFLOW_FILE}`]({WORKFLOW_FILE})",
        )
    return lines


def _render_claim_row(root: Path, claim: PacketClaim) -> list[str]:
    return [
        _escape_cell(claim.label),
        f"`{_render_cell(claim.value)}`",
        f"{_link_stage(root, claim.stage_id)} · `{claim.column}`",
    ]


def _render_sources(contents: PacketContents) -> list[str]:
    if not contents.sources:
        return []
    return [
        "## Sources this run read",
        "",
        "Every figure above descends from these files and nothing else. The hash is what "
        f"the run recorded as it opened the file; the copy under `{INPUTS_DIR}/` is the "
        "bytes it read.",
        "",
        *_render_table(
            ["Source", "Read as", "Rows", "Size", "SHA-256"],
            [_render_source_row(contents.root, source) for source in contents.sources],
            f"the rest are in [`{MANIFEST_FILE}`]({MANIFEST_FILE})",
        ),
    ]


def _render_source_row(root: Path, source: PacketSource) -> list[str]:
    binding = source.binding
    # The basename only: `path` is an absolute path on the machine that ran it.
    name = f"`{_escape_cell(binding.filename)}`"
    return [
        f"[{name}]({source.copy_path})" if source.copy_path else name,
        _link_stage(root, binding.stage_id),
        _render_count(source.row_count),
        _describe_bytes(binding.bytes) if binding.bytes is not None else "unknown",
        f"`{binding.sha256}`" if binding.sha256 else "unrecorded",
    ]


def _render_production(contents: PacketContents) -> list[str]:
    run = contents.run
    version = run.workflow_version or "unrecorded"
    finished = f"finished {run.finished_at}" if run.finished_at else "never finished"
    return [
        "## How it was produced",
        "",
        f"- **Project** `{run.project}` · **run** `{run.run_id}` · "
        f"**workflow version** `{version}`",
        f"- Started {run.started_at}, {finished} · status **{run.status}**",
        f"- {_render_step_split(contents)}",
        f"- The method as prose: [`{DOCUMENT_FILE}`]({DOCUMENT_FILE}) · the pinned "
        f"workflow: [`{WORKFLOW_FILE}`]({WORKFLOW_FILE})",
        "",
    ]


def _render_step_split(contents: PacketContents) -> str:
    if not contents.steps:
        return "The workflow this run pinned could not be read back, so its steps are unlisted"
    called_a_model = _count_steps_of_type(contents, StageType.llm_transform)
    asked_a_person = _count_steps_of_type(contents, StageType.human_review_queue)
    code = len(contents.steps) - called_a_model - asked_a_person
    return (
        f"**{len(contents.steps)} steps**: {called_a_model} called an AI model, "
        f"{asked_a_person} put a row to a person, {code} are plain code"
    )


def _count_steps_of_type(contents: PacketContents, stage_type: StageType) -> int:
    return sum(1 for step in contents.steps if step.type == stage_type.value)


def _render_flags(contents: PacketContents) -> list[str]:
    if not contents.flags:
        return []
    return [
        f"## What the run flagged about itself ({len(contents.flags)})",
        "",
        *_render_table(
            ["Step", "Severity", "Column", "What it says"],
            [_render_flag_row(contents.root, flag) for flag in contents.flags],
            f"the rest are in [`{MANIFEST_FILE}`]({MANIFEST_FILE})",
        ),
    ]


def _render_flag_row(root: Path, flag: PacketFlag) -> list[str]:
    return [
        _link_stage(root, flag.stage_id),
        flag.severity,
        f"`{_escape_cell(flag.column)}`" if flag.column else "—",
        _escape_cell(_truncate(flag.message, MAX_FLAG_CHARS)),
    ]


def _render_steps(contents: PacketContents) -> list[str]:
    if not contents.steps:
        return []
    return [
        "## The steps",
        "",
        "Ordered along each branch: one line of work is carried to the point where it "
        "meets another, then the branch it was waiting for, then the step that joins them.",
        "",
        *_render_table(
            ["Step", "Kind", "Rows out", "Joins", "What it does"],
            [_render_step_row(contents.root, step) for step in contents.steps],
            f"the rest are in [`{WORKFLOW_FILE}`]({WORKFLOW_FILE})",
        ),
    ]


def _render_step_row(root: Path, step: PacketStep) -> list[str]:
    return [
        _link_stage(root, step.stage_id),
        f"`{step.type}`",
        _render_count(step.row_count),
        _render_joins(step.parent_ids),
        _escape_cell(step.description),
    ]


def _render_joins(parent_ids: list[ID]) -> str:
    if len(parent_ids) < 2:
        return ""
    named = " + ".join(f"`{parent}`" for parent in parent_ids[:MAX_NAMED_PARENTS])
    over = len(parent_ids) - MAX_NAMED_PARENTS
    return f"{named} + {over} more" if over > 0 else named


def _render_folder(contents: PacketContents) -> list[str]:
    rows = [
        *_render_recorded_rows(contents.root),
        *_render_rendering_rows(contents.root),
    ]
    return [
        "## What is in this folder",
        "",
        *_render_table(["Path", "What it is", "Authoritative"], rows, ""),
    ]


def _render_recorded_rows(root: Path) -> list[list[str]]:
    return [
        [f"`{path}`", role, held]
        for path, role, held in _RECORDED_LAYOUT
        if path in _WRITTEN_LAST or (root / path).exists()
    ]


def _render_rendering_rows(root: Path) -> list[list[str]]:
    """Whatever else the export left here: the app's own pages, which re-show the above."""
    recorded = {path.split("/")[0] for path, _, _ in _RECORDED_LAYOUT}
    return [
        [f"`{entry.name}{'/' if entry.is_dir() else ''}`", _A_RENDERING, _A_RENDERING_HELD]
        for entry in sorted(root.iterdir(), key=lambda p: p.name)
        if entry.name not in recorded
    ]


_A_RENDERING = "A rendered view of the files above"
_A_RENDERING_HELD = "no"

_RECORDED_LAYOUT: tuple[tuple[str, str, str], ...] = (
    (f"{DATA_DIR}/", "Every step's full output, one CSV each", "yes"),
    (f"{RAW_DIR}/", "The same outputs as this app wrote them, types intact", "yes"),
    (f"{INPUTS_DIR}/", "The source files, as read, at the recorded SHA-256", "yes"),
    (f"{ARTIFACTS_DIR}/", "What the publish step wrote", "yes"),
    (MANIFEST_FILE, "The run's own record: statuses, row counts, validation", "yes"),
    (WORKFLOW_FILE, "The workflow version this run executed", "yes"),
    (DOCUMENT_FILE, "The prose the workflow was compiled from", "yes"),
    (EVENTS_FILE, "The run's event log, with what any AI model was asked", "yes"),
    (CHECKSUMS_FILE, "SHA-256 of every file here but itself", "yes"),
    (README_FILE, "This page", _A_RENDERING_HELD),
)

# Written after this page is composed, so neither is on disk to be found.
_WRITTEN_LAST = (CHECKSUMS_FILE, README_FILE)


# ── Cells ────────────────────────────────────────────────────────────────────

# Grouping is applied only to a plain number with no leading zero: `0001` is an
# identifier a CSV lost the type of, and `1` would be a different value.
_PLAIN_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")


def _render_cell(cell: str | None) -> str:
    if cell is None:
        return "unknown"
    grouped = _group_digits(cell)
    return _escape_cell(cell if grouped is None else grouped)


def _group_digits(cell: str) -> str | None:
    if not _PLAIN_NUMBER.fullmatch(cell):
        return None
    try:
        number = Decimal(cell)
    except InvalidOperation:
        return None
    grouped = f"{number:,f}" if "." in cell else f"{number:,}"
    if grouped.endswith(".0"):
        grouped = grouped[:-2]
    return grouped if Decimal(grouped.replace(",", "")) == number else None


def _render_count(count: int | None) -> str:
    return "—" if count is None else f"{count:,}"


def _describe_bytes(count: int) -> str:
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if count >= size:
            return f"{count / size:.3g} {unit}"
    return f"{count} B"


def _truncate(text: str, ceiling: int) -> str:
    if len(text) <= ceiling:
        return text
    return text[:ceiling].rsplit(" ", 1)[0] + " …"


def _escape_cell(text: str) -> str:
    """A pipe or a newline in a value would end the markdown cell it sits in."""
    return text.replace("|", "\\|").replace("\n", " ")


def _link_stage(root: Path, stage_id: ID) -> str:
    """A step with no CSV in the packet is named but not linked — the file is not there."""
    relative = find_data_file(root, stage_id)
    return f"[`{stage_id}`]({relative})" if relative else f"`{stage_id}`"


def _render_table(headers: list[str], rows: list[list[str]], uncapped: str) -> list[str]:
    shown = rows[:MAX_TABLE_ROWS]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
        *("| " + " | ".join(row) + " |" for row in shown),
        "",
    ]
    if len(rows) > len(shown):
        lines += [f"The first {len(shown):,} of {len(rows):,} rows are shown; {uncapped}.", ""]
    return lines
