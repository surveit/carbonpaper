"""Serving a run directory's events.jsonl to the log panel: the opening tail, the
walk back through older events, and the SSE stream. Written against a run DIRECTORY,
so a production run under runs/ and an eval's subset run under eval_run/ are the same
thing to it — both hold an events.jsonl the same writer produced.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Request

from app.core.run_status import RunStatus
from app.runtime.run_log import RUN_DONE, read_events_since
from app.web.loading import load_manifest

# How the SSE tail polls events.jsonl, and how many empty polls it tolerates after
# the manifest has settled before it stops a stream whose run_done never arrived.
_EVENT_POLL_INTERVAL_S = 0.5
_IDLE_POLLS_BEFORE_TERMINAL_STOP = 2

# A ceiling on what one "load older" fetch may ask for; the default page size is
# EVENT_TAIL, in app.web.config, because the stage panel's log is sized by it too.
EVENT_PAGE_MAX = 5000


def select_stage_events(
    events: list[dict[str, Any]], stage: str | None
) -> list[dict[str, Any]]:
    if stage is None:
        return events
    # RUN_DONE rides through the filter: it is what ends an SSE stream, so a
    # scoped feed that dropped it would tail a finished run forever.
    return [
        event
        for event in events
        if event.get("stage") == stage or event.get("kind") == RUN_DONE
    ]


def tail_start_seq(events_path: Path, tail: int, stage: str | None = None) -> int:
    """Counts parsed events rather than `highest - tail`: seq is not guaranteed gap-free."""
    events = select_stage_events(read_events_since(events_path, 0), stage)
    if not events:
        return 0
    if tail <= 0:
        return int(events[-1]["seq"]) + 1      # start past the end: nothing old
    return 0 if len(events) <= tail else int(events[-tail]["seq"])


def page_events_before(
    events_path: Path, before_seq: int, limit: int, stage: str | None
) -> dict[str, Any]:
    older = [
        event
        for event in select_stage_events(read_events_since(events_path, 0), stage)
        if int(event["seq"]) < before_seq
    ]
    # The window is cut AFTER filtering, not from `before_seq - limit`: a stage
    # holding a handful of events inside a 5000-seq span would otherwise hand
    # back a nearly empty page and report the rest as already loaded.
    page = older[-limit:]
    first_seq = int(page[0]["seq"]) if page else 0
    return {
        "events": page,
        "first_seq": first_seq,
        "has_more": len(older) > len(page),
    }


async def stream_events(
    run_dir: Path, request: Request, from_seq: int, stage: str | None = None
) -> AsyncIterator[str]:
    """Polls the file: the run executes on worker threads with no access to this loop."""
    events_path = run_dir / "events.jsonl"
    cursor = from_seq
    idle_polls = 0
    while True:
        if await request.is_disconnected():
            return
        new = read_events_since(events_path, cursor)
        # The cursor clears the whole batch that was READ, not the subset the
        # stage filter yields: an event the filter drops must not come back on
        # the next poll.
        if new:
            cursor = int(new[-1]["seq"]) + 1
        for event in select_stage_events(new, stage):
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("kind") == RUN_DONE:
                return
        # Fallback stop: if the writer never wrote run_done (a crash mid-run),
        # end once the manifest has settled AND a couple of polls added nothing,
        # so a client never hangs on an interrupted run.
        if _find_terminal_status(run_dir) is not None:
            idle_polls = 0 if new else idle_polls + 1
            if idle_polls >= _IDLE_POLLS_BEFORE_TERMINAL_STOP:
                yield "event: done\ndata: {}\n\n"
                return
        await asyncio.sleep(_EVENT_POLL_INTERVAL_S)


def _find_terminal_status(run_dir: Path) -> str | None:
    status = load_manifest(run_dir).get("status")
    return None if status == RunStatus.RUNNING else status
