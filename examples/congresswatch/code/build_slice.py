"""
Build input CSVs for the CongressWatch methodology from the raw datasets
in ~/Downloads/data/data/.

Outputs (all under examples/congresswatch/data/):
  members.csv             — one row per member of Congress active in the slice
  press_corpus.csv        — press releases (entity_id keyed on member.bioguide_id)
  lobbying_filings.csv    — lobbying disclosures filtered to slice quarter + issue codes

Slice definition is read from environment vars; defaults below.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET


DOWNLOADS = Path(os.path.expanduser("~/Downloads/data/data"))
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ─── Slice config ────────────────────────────────────────────────────────────

PRESS_MONTH = os.environ.get("CW_PRESS_MONTH", "2026-01")
LOB_QUARTERS = os.environ.get("CW_LOB_QUARTERS", "2025_4thQuarter").split(",")
ISSUE_CODES = os.environ.get("CW_ISSUE_CODES", "HCR,INS,MED,MMM,PHA").split(",")
TOPIC_PATTERNS = [
    r"\bACA\b", r"premium tax credit", r"enhanced.*tax credit",
    r"\bAffordable Care\b", r"health.{0,3}care cost", r"\bMedicare\b",
    r"\bMedicaid\b", r"prescription drug",
]
TOPIC_RE = re.compile("|".join(TOPIC_PATTERNS), re.IGNORECASE)


# ─── Press release slice ─────────────────────────────────────────────────────

def slice_press() -> tuple[list[dict], dict[str, dict]]:
    """Return (matching press records, members-keyed-by-bioguide)."""
    path = DOWNLOADS / "congress_press" / f"{PRESS_MONTH}.jsonl"
    records: list[dict] = []
    members: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = (rec.get("text") or "") + " " + (rec.get("title") or "")
            if not TOPIC_RE.search(text):
                continue
            member = rec.get("member") or {}
            bid = member.get("bioguide_id")
            if not bid:
                continue
            records.append(rec)
            members.setdefault(bid, {
                "entity_id": f"M:{bid}",
                "bioguide_id": bid,
                "name": member.get("name") or "",
                "state": member.get("state") or "",
                "party": member.get("party") or "",
                "chamber": member.get("chamber") or "",
            })
    return records, members


def write_press_corpus(records: list[dict]) -> None:
    path = OUT_DIR / "press_corpus.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["doc_id", "entity_id", "source_class", "title", "body",
                    "published_at", "url"])
        for rec in records:
            member = rec.get("member") or {}
            bid = member.get("bioguide_id")
            doc_id = f"PR_{bid}_{rec.get('date','')}_{abs(hash(rec.get('url',''))) % 100000:05d}"
            w.writerow([
                doc_id,
                f"M:{bid}",
                "press_release",
                rec.get("title", ""),
                rec.get("text", ""),
                rec.get("date", ""),
                rec.get("url", ""),
            ])
    print(f"wrote {path} ({len(records)} rows)")


def write_members(members: dict[str, dict]) -> None:
    path = OUT_DIR / "members.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["entity_id", "bioguide_id", "name", "state", "party", "chamber"],
        )
        w.writeheader()
        for m in sorted(members.values(), key=lambda x: x["entity_id"]):
            w.writerow(m)
    print(f"wrote {path} ({len(members)} rows)")


# ─── Lobbying slice ──────────────────────────────────────────────────────────

def parse_house_xml(path: Path) -> dict | None:
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return None
    root = tree.getroot()
    if (root.findtext("noLobbying") or "").strip() == "Y":
        return None
    codes = [el.text for el in root.iter("issueAreaCode") if el.text]
    if not codes:
        return None
    if not any(c in ISSUE_CODES for c in codes):
        return None
    issues = " | ".join(
        (el.text or "").strip()
        for el in root.iter("specific_issues")
        if el.text and el.text.strip()
    )
    lobbyists = []
    for lob in root.iter("lobbyist"):
        fn = (lob.findtext("lobbyistFirstName") or "").strip()
        ln = (lob.findtext("lobbyistLastName") or "").strip()
        if fn or ln:
            lobbyists.append(f"{fn} {ln}".strip())
    return {
        "filing_id": root.findtext("houseID") or path.stem,
        "filer_org": (root.findtext("organizationName") or "").strip(),
        "client_name": (root.findtext("clientName") or "").strip(),
        "issue_codes": ";".join(codes),
        "issues_text": issues[:8000],
        "income": (root.findtext("income") or "").strip(),
        "expenses": (root.findtext("expenses") or "").strip(),
        "report_year": (root.findtext("reportYear") or "").strip(),
        "report_type": (root.findtext("reportType") or "").strip(),
        "lobbyists": " | ".join(lobbyists[:10]),
        "source_path": str(path.relative_to(DOWNLOADS)),
    }


def slice_lobbying() -> list[dict]:
    results: list[dict] = []
    for quarter in LOB_QUARTERS:
        d = DOWNLOADS / "house" / f"{quarter}_XML"
        if not d.is_dir():
            print(f"skip missing: {d}")
            continue
        files = sorted(d.iterdir())
        print(f"scanning {quarter}: {len(files)} files...")
        for i, p in enumerate(files):
            if i % 5000 == 0 and i:
                print(f"  ...{i}")
            row = parse_house_xml(p)
            if row:
                results.append(row)
    print(f"matched {len(results)} filings on codes {ISSUE_CODES}")
    return results


def write_lobbying(rows: list[dict]) -> None:
    path = OUT_DIR / "lobbying_filings.csv"
    cols = ["filing_id", "filer_org", "client_name", "issue_codes",
            "issues_text", "income", "expenses", "report_year",
            "report_type", "lobbyists", "source_path"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> int:
    print(f"slice: press={PRESS_MONTH}, lobbying={LOB_QUARTERS}, codes={ISSUE_CODES}")
    print()
    records, members = slice_press()
    print(f"press: {len(records)} records, {len(members)} unique members")
    write_press_corpus(records)
    write_members(members)
    print()
    rows = slice_lobbying()
    write_lobbying(rows)
    print()
    print(f"all outputs in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
