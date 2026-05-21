"""
Roldugin de-risking run.

Manually traces Sergei Roldugin from OpenSanctions PEPs into ICIJ Offshore Leaks
and out to his published Panama Papers shell companies. If this works with stdlib
streaming, the planned prototype has legs. If it doesn't, we rethink.

Stdlib only. No pandas, no networkx — we want to feel the raw file sizes.
"""

import csv
import json
import sys
import time
from pathlib import Path

# Force UTF-8 stdout — Windows default cp1252 chokes on arrows etc.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Increase CSV field size — some ICIJ rows have long address fields.
csv.field_size_limit(10_000_000)

DOWNLOADS = Path(r"C:\Users\shuha\Downloads")
PEP_FILE = DOWNLOADS / "pep.ftm.json"
OFFICERS_CSV = DOWNLOADS / "nodes-officers.csv"
ENTITIES_CSV = DOWNLOADS / "nodes-entities.csv"
RELS_CSV = DOWNLOADS / "relationships.csv"
ADDRESSES_CSV = DOWNLOADS / "nodes-addresses.csv"
INTERMEDIARIES_CSV = DOWNLOADS / "nodes-intermediaries.csv"

NEEDLES = ("roldugin", "roldugine", "rolduguine")

# Three companies that ICIJ's published Panama Papers reporting linked to Roldugin.
PUBLISHED_COMPANIES = [
    "Sandalwood Continental",
    "Sonnette Overseas",
    "International Media Overseas",
]


def matches_roldugin(text):
    if not text:
        return False
    low = text.lower()
    return any(n in low for n in NEEDLES)


def step1_pep():
    print("=" * 70)
    print("STEP 1 — OpenSanctions PEPs: search for Roldugin")
    print("=" * 70)
    t0 = time.perf_counter()
    matches = []
    person_count = 0
    line_count = 0
    with PEP_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line_count += 1
            try:
                ent = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ent.get("schema") != "Person":
                continue
            person_count += 1
            caption = ent.get("caption", "") or ""
            names = ent.get("properties", {}).get("name", []) or []
            if matches_roldugin(caption) or any(matches_roldugin(n) for n in names):
                matches.append(ent)
    elapsed = time.perf_counter() - t0
    print(f"Streamed {line_count:,} lines, {person_count:,} Person entities, in {elapsed:.1f}s")
    print(f"Roldugin matches: {len(matches)}")
    for m in matches:
        props = m.get("properties", {})
        print()
        print(f"  id:         {m.get('id')}")
        print(f"  caption:    {m.get('caption')}")
        print(f"  names:      {props.get('name')}")
        print(f"  country:    {props.get('country')}")
        print(f"  position:   {props.get('position')}")
        print(f"  topics:     {props.get('topics')}")
        print(f"  datasets:   {m.get('datasets')}")
    return matches


def step2_officers():
    print()
    print("=" * 70)
    print("STEP 2 — ICIJ officers: search for Roldugin")
    print("=" * 70)
    t0 = time.perf_counter()
    matches = []
    row_count = 0
    with OFFICERS_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_count += 1
            if matches_roldugin(row.get("name", "")):
                matches.append(row)
    elapsed = time.perf_counter() - t0
    print(f"Streamed {row_count:,} officer rows in {elapsed:.1f}s")
    print(f"Roldugin matches: {len(matches)}")
    for r in matches:
        print()
        print(f"  node_id:    {r['node_id']}")
        print(f"  name:       {r['name']}")
        print(f"  countries:  {r['countries']}")
        print(f"  sourceID:   {r['sourceID']}")
    return matches


def step3_eyeball(pep_matches, officer_matches):
    print()
    print("=" * 70)
    print("STEP 3 — Eyeball link")
    print("=" * 70)
    if not pep_matches:
        print("No PEP match — can't link. STOP.")
        return
    if not officer_matches:
        print("No officer match — can't link. STOP.")
        return
    print(f"PEP record(s): {[m.get('caption') for m in pep_matches]}")
    print(f"ICIJ officer(s): {[r['name'] for r in officer_matches]}")
    print("(Manual eyeball — for Roldugin the rare name makes this trivial.")
    print(" Note for Layer 1 design: general case needs fuzzy match across transliterations.)")


def step4_subgraph(officer_matches):
    print()
    print()
    print("=" * 70)
    print("STEP 4 — 1-hop subgraph from Roldugin officer node(s)")
    print("=" * 70)
    if not officer_matches:
        print("No officer ids — skipping.")
        return [], []

    officer_ids = {r["node_id"] for r in officer_matches}
    print(f"Officer node_ids: {sorted(officer_ids)}")

    # Pass 1: relationships
    t0 = time.perf_counter()
    edges = []
    neighbor_ids = set()
    rel_count = 0
    with RELS_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rel_count += 1
            start = row["node_id_start"]
            end = row["node_id_end"]
            if start in officer_ids:
                edges.append((start, end, row["rel_type"], "out", row.get("start_date"), row.get("end_date"), row.get("sourceID")))
                neighbor_ids.add(end)
            elif end in officer_ids:
                edges.append((start, end, row["rel_type"], "in", row.get("start_date"), row.get("end_date"), row.get("sourceID")))
                neighbor_ids.add(start)
    print(f"Streamed {rel_count:,} relationship rows in {time.perf_counter()-t0:.1f}s")
    print(f"Edges touching Roldugin: {len(edges)}")
    print(f"Distinct neighbor node_ids: {len(neighbor_ids)}")

    # Pass 2: entity lookup
    t0 = time.perf_counter()
    entity_lookup = {}
    with ENTITIES_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["node_id"] in neighbor_ids:
                entity_lookup[row["node_id"]] = row
    print(f"Entity lookup pass in {time.perf_counter()-t0:.1f}s — matched {len(entity_lookup)} entities")

    # Pass 3: address lookup
    t0 = time.perf_counter()
    address_lookup = {}
    with ADDRESSES_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["node_id"] in neighbor_ids:
                address_lookup[row["node_id"]] = row
    print(f"Address lookup pass in {time.perf_counter()-t0:.1f}s — matched {len(address_lookup)} addresses")

    # Pass 4: intermediaries
    t0 = time.perf_counter()
    intermediary_lookup = {}
    with INTERMEDIARIES_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["node_id"] in neighbor_ids:
                intermediary_lookup[row["node_id"]] = row
    print(f"Intermediary lookup pass in {time.perf_counter()-t0:.1f}s — matched {len(intermediary_lookup)} intermediaries")

    # Render
    print()
    print("--- Subgraph ---")
    print(f"{'rel_type':<22} {'kind':<14} {'name':<48} {'jurisdiction':<14} {'status':<14} {'sourceID':<32}")
    print("-" * 150)
    rows_for_step5 = []
    for start, end, rel_type, direction, start_date, end_date, src in edges:
        nbr_id = end if direction == "out" else start
        if nbr_id in entity_lookup:
            e = entity_lookup[nbr_id]
            kind = "Entity"
            name = e.get("name") or e.get("original_name") or ""
            jurisdiction = e.get("jurisdiction_description") or e.get("jurisdiction") or ""
            status = e.get("status") or ""
            sid = e.get("sourceID") or ""
        elif nbr_id in address_lookup:
            a = address_lookup[nbr_id]
            kind = "Address"
            name = a.get("address") or ""
            jurisdiction = a.get("countries") or ""
            status = ""
            sid = a.get("sourceID") or ""
        elif nbr_id in intermediary_lookup:
            i = intermediary_lookup[nbr_id]
            kind = "Intermediary"
            name = i.get("name") or ""
            jurisdiction = i.get("countries") or ""
            status = i.get("status") or ""
            sid = i.get("sourceID") or ""
        else:
            kind = "Unknown"
            name = nbr_id
            jurisdiction = ""
            status = ""
            sid = ""
        print(f"{rel_type:<22} {kind:<14} {name[:46]:<48} {jurisdiction[:12]:<14} {status[:12]:<14} {sid[:30]:<32}")
        rows_for_step5.append((kind, name, jurisdiction, status, sid))
    return edges, rows_for_step5


def step5_compare(rows):
    print()
    print("=" * 70)
    print("STEP 5 — Compare to published Panama Papers findings")
    print("=" * 70)
    found = {}
    extras = []
    for kind, name, jurisdiction, status, sid in rows:
        if kind != "Entity":
            continue
        matched_published = False
        for needle in PUBLISHED_COMPANIES:
            if needle.lower() in (name or "").lower():
                found.setdefault(needle, []).append((name, jurisdiction, status, sid))
                matched_published = True
        if not matched_published:
            extras.append((name, jurisdiction, status, sid))

    print()
    print("Published companies status:")
    hit = 0
    for needle in PUBLISHED_COMPANIES:
        if needle in found:
            hit += 1
            for n, j, s, sid in found[needle]:
                print(f"  [HIT]  {needle:<32} → {n} ({j}, {s}, {sid})")
        else:
            print(f"  [MISS] {needle}")
    print()
    print(f"PASS/FAIL: {hit}/{len(PUBLISHED_COMPANIES)} published companies appear in 1-hop subgraph")
    print()
    print(f"Other entities in subgraph (candidate 'why was this here?' items): {len(extras)}")
    for n, j, s, sid in extras:
        print(f"  - {n}  ({j}, {s}, {sid})")


def main():
    overall_t0 = time.perf_counter()
    pep_matches = step1_pep()
    officer_matches = step2_officers()
    step3_eyeball(pep_matches, officer_matches)
    edges, rows = step4_subgraph(officer_matches)
    step5_compare(rows)
    print()
    print(f"Total wall time: {time.perf_counter()-overall_t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main())
