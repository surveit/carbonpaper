"""Replay require_pr_url at each Stop recorded in saved Claude Code transcripts, and count its blocks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import NamedTuple

from scripts.claude_hooks.require_pr_url import (
    find_missing_pr_urls,
    is_pr_written_in_last_turn,
    is_stop_summary,
    read_transcript,
)


class StopCounts(NamedTuple):
    transcripts: int
    stops: int
    stops_after_a_pr_write: int
    would_block: int


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude" / "projects")
    args = parser.parse_args(argv)
    print(json.dumps(count_stops(sorted(args.projects_dir.glob("*/*.jsonl")))._asdict()))
    return 0


def count_stops(transcripts: list[Path]) -> StopCounts:
    stops = stops_after_a_pr_write = blocks = 0
    for transcript in transcripts:
        entries = read_transcript(str(transcript)) or []
        for index, entry in enumerate(entries):
            if not is_stop_summary(entry):
                continue
            stops += 1
            stops_after_a_pr_write += is_pr_written_in_last_turn(entries[:index])
            blocks += bool(find_missing_pr_urls(entries[:index]))
    return StopCounts(len(transcripts), stops, stops_after_a_pr_write, blocks)


if __name__ == "__main__":
    raise SystemExit(main())
