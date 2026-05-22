"""
Build two-window slice data for the drift methodology.

Outputs (under examples/drift/data/):
  members.csv               — members active in BOTH windows
  press_window_early.csv    — press releases in the earlier window
  press_window_recent.csv   — press releases in the recent window

Slice config (env vars, all optional):
  DRIFT_EARLY            : "2025-09"     - YYYY-MM of earlier window (file lookup)
  DRIFT_RECENT           : "2026-01"     - YYYY-MM of recent window
  DRIFT_MIN_PER_WINDOW   : "3"           - min press releases per member per window
  DRIFT_MAX_MEMBERS      : ""            - cap members for cheap test runs
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from pathlib import Path


DOWNLOADS = Path(os.path.expanduser("~/Downloads/data/data"))
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)


EARLY = os.environ.get("DRIFT_EARLY", "2025-09")
RECENT = os.environ.get("DRIFT_RECENT", "2026-01")
MIN_PER_WINDOW = int(os.environ.get("DRIFT_MIN_PER_WINDOW", "3"))
_MAX = os.environ.get("DRIFT_MAX_MEMBERS", "").strip()
MAX_MEMBERS: int | None = int(_MAX) if _MAX else None


def _press_path_for(period: str) -> Path:
    """Locate the JSONL file for a YYYY-MM. May live at top level (2026 files)
    or in a YYYY/ subfolder (2022-2025 files split by year)."""
    direct = DOWNLOADS / "congress_press" / f"{period}.jsonl"
    if direct.exists():
        return direct
    year = period.split("-")[0]
    subfolder = DOWNLOADS / "congress_press" / year
    if subfolder.is_dir():
        for candidate in subfolder.iterdir():
            if candidate.name.startswith(period):
                return candidate
    raise FileNotFoundError(f"No press file for period {period}")


def _load_window(period: str) -> list[dict]:
    path = _press_path_for(period)
    records = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            member = rec.get("member") or {}
            if member.get("bioguide_id"):
                records.append(rec)
    return records


def main() -> int:
    print(f"early window:  {EARLY}")
    print(f"recent window: {RECENT}")
    print(f"min per window: {MIN_PER_WINDOW}")
    if MAX_MEMBERS:
        print(f"capping members at: {MAX_MEMBERS}")
    print()

    early = _load_window(EARLY)
    recent = _load_window(RECENT)
    print(f"loaded {len(early)} early + {len(recent)} recent records")

    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"early": 0, "recent": 0})
    for rec in early:
        counts[rec["member"]["bioguide_id"]]["early"] += 1
    for rec in recent:
        counts[rec["member"]["bioguide_id"]]["recent"] += 1

    eligible = {
        bid for bid, c in counts.items()
        if c["early"] >= MIN_PER_WINDOW and c["recent"] >= MIN_PER_WINDOW
    }
    print(f"{len(eligible)} members have >= {MIN_PER_WINDOW} releases in BOTH windows")

    if MAX_MEMBERS and len(eligible) > MAX_MEMBERS:
        # Prioritise members with the most material to compare.
        ranked = sorted(
            eligible,
            key=lambda b: counts[b]["early"] + counts[b]["recent"],
            reverse=True,
        )
        eligible = set(ranked[:MAX_MEMBERS])
        print(f"capped to top {MAX_MEMBERS} most-active: {len(eligible)} members")

    # Member roster
    member_rows: dict[str, dict] = {}
    for rec in early + recent:
        m = rec["member"]
        bid = m.get("bioguide_id")
        if bid not in eligible:
            continue
        member_rows.setdefault(bid, {
            "entity_id": f"M:{bid}",
            "bioguide_id": bid,
            "name": m.get("name", ""),
            "state": m.get("state", ""),
            "party": m.get("party", ""),
            "chamber": m.get("chamber", ""),
            "early_release_count": counts[bid]["early"],
            "recent_release_count": counts[bid]["recent"],
        })
    members_path = OUT_DIR / "members.csv"
    with members_path.open("w", encoding="utf-8", newline="") as fh:
        cols = ["entity_id", "bioguide_id", "name", "state", "party", "chamber",
                "early_release_count", "recent_release_count"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for row in sorted(member_rows.values(), key=lambda r: r["entity_id"]):
            w.writerow(row)
    print(f"wrote {members_path} ({len(member_rows)} rows)")

    def _write_press(period_records: list[dict], path: Path, window_label: str) -> None:
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["doc_id", "entity_id", "window", "title", "body",
                        "published_at", "url"])
            for rec in period_records:
                bid = rec["member"]["bioguide_id"]
                if bid not in eligible:
                    continue
                doc_id = f"PR_{bid}_{rec.get('date', '')}_{abs(hash(rec.get('url', ''))) % 100000:05d}"
                w.writerow([
                    doc_id,
                    f"M:{bid}",
                    window_label,
                    rec.get("title", ""),
                    rec.get("text", ""),
                    rec.get("date", ""),
                    rec.get("url", ""),
                ])

    _write_press(early, OUT_DIR / "press_window_early.csv", "early")
    _write_press(recent, OUT_DIR / "press_window_recent.csv", "recent")
    print(f"wrote press CSVs to {OUT_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
