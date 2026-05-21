"""
Second de-risking test: Assad family network (Panama Papers).

Bashar al-Assad himself isn't typically an officer in the leaks. The exposure ran
through his cousin Rami Makhlouf (sanctioned by US Treasury since 2008 for being
the regime's financial frontman), and through his security-services cousin
Hafez Makhlouf. Rami was famously named with Drex Technologies S.A. (Seychelles).

Same shape as test_roldugin.py: stream PEP file, stream officers, traverse 1-hop,
compare against published findings.
"""

import csv
import json
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

csv.field_size_limit(10_000_000)

DOWNLOADS = Path(r"C:\Users\shuha\Downloads")
PEP_FILE = DOWNLOADS / "pep.ftm.json"
OFFICERS_CSV = DOWNLOADS / "nodes-officers.csv"
ENTITIES_CSV = DOWNLOADS / "nodes-entities.csv"
RELS_CSV = DOWNLOADS / "relationships.csv"
ADDRESSES_CSV = DOWNLOADS / "nodes-addresses.csv"
INTERMEDIARIES_CSV = DOWNLOADS / "nodes-intermediaries.csv"

# We're casting a wider net here than for Roldugin — Assad family network spans
# multiple surnames. Keep these distinct so we can tell who came from where.
NEEDLES = {
    "assad": ["assad", "asad"],
    "makhlouf": ["makhlouf", "makhluf", "makhlouf"],
    # Other Assad-circle offshore names that surfaced in Panama Papers reporting
    "shalish": ["shalish"],  # Dhu al-Himma Shalish, Bashar's cousin / arms procurer
}

# Companies the published Panama Papers reporting tied to Rami Makhlouf
PUBLISHED_COMPANIES = [
    "Drex Technologies",
    "Ramak",
    "Lema Trading",
]


def matches(text, needles):
    if not text:
        return None
    low = text.lower()
    for surname, variants in needles.items():
        for v in variants:
            if v in low:
                return surname
    return None


def step1_pep():
    print("=" * 70)
    print("STEP 1 — OpenSanctions PEPs: search for Assad family")
    print("=" * 70)
    t0 = time.perf_counter()
    matches_by_surname = {k: [] for k in NEEDLES}
    line_count = 0
    person_count = 0
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
            hit = matches(caption, NEEDLES)
            if not hit:
                for n in names:
                    hit = matches(n, NEEDLES)
                    if hit:
                        break
            if hit:
                matches_by_surname[hit].append(ent)
    elapsed = time.perf_counter() - t0
    print(f"Streamed {line_count:,} lines, {person_count:,} Person entities, in {elapsed:.1f}s")
    for surname, hits in matches_by_surname.items():
        print(f"\n  {surname}: {len(hits)} matches")
        for m in hits[:5]:  # cap noise — top 5 per surname
            props = m.get("properties", {})
            print(f"    {m.get('caption')}  | country={props.get('country')} | topics={props.get('topics')} | id={m.get('id')}")
        if len(hits) > 5:
            print(f"    ... +{len(hits)-5} more")
    return matches_by_surname


def step2_officers():
    print()
    print("=" * 70)
    print("STEP 2 — ICIJ officers: search for Assad family")
    print("=" * 70)
    t0 = time.perf_counter()
    matches_by_surname = {k: [] for k in NEEDLES}
    row_count = 0
    with OFFICERS_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            row_count += 1
            hit = matches(row.get("name", ""), NEEDLES)
            if hit:
                matches_by_surname[hit].append(row)
    elapsed = time.perf_counter() - t0
    print(f"Streamed {row_count:,} officer rows in {elapsed:.1f}s")
    for surname, hits in matches_by_surname.items():
        print(f"\n  {surname}: {len(hits)} officer node(s)")
        for r in hits:
            print(f"    {r['node_id']:>10}  {r['name']:<40}  {r['countries']:<20}  {r['sourceID']}")
    return matches_by_surname


def step4_subgraph(officer_matches_by_surname):
    print()
    print("=" * 70)
    print("STEP 4 — 1-hop subgraph from all matched officer nodes")
    print("=" * 70)

    officer_ids_by_surname = {s: {r["node_id"] for r in hits} for s, hits in officer_matches_by_surname.items()}
    all_officer_ids = set()
    for ids in officer_ids_by_surname.values():
        all_officer_ids |= ids
    if not all_officer_ids:
        print("No officer ids — skipping.")
        return []

    print(f"Total officer node_ids: {len(all_officer_ids)}")

    t0 = time.perf_counter()
    edges = []
    neighbor_ids = set()
    with RELS_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            s, e = row["node_id_start"], row["node_id_end"]
            if s in all_officer_ids:
                edges.append((s, e, row["rel_type"], "out", row.get("start_date"), row.get("sourceID")))
                neighbor_ids.add(e)
            elif e in all_officer_ids:
                edges.append((s, e, row["rel_type"], "in", row.get("start_date"), row.get("sourceID")))
                neighbor_ids.add(s)
    print(f"Relationships pass in {time.perf_counter()-t0:.1f}s — {len(edges)} edges, {len(neighbor_ids)} distinct neighbors")

    t0 = time.perf_counter()
    entity_lookup = {}
    with ENTITIES_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["node_id"] in neighbor_ids:
                entity_lookup[row["node_id"]] = row
    print(f"Entity lookup in {time.perf_counter()-t0:.1f}s — {len(entity_lookup)} entities")

    t0 = time.perf_counter()
    address_lookup = {}
    with ADDRESSES_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["node_id"] in neighbor_ids:
                address_lookup[row["node_id"]] = row
    print(f"Address lookup in {time.perf_counter()-t0:.1f}s — {len(address_lookup)} addresses")

    t0 = time.perf_counter()
    intermediary_lookup = {}
    with INTERMEDIARIES_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["node_id"] in neighbor_ids:
                intermediary_lookup[row["node_id"]] = row
    print(f"Intermediary lookup in {time.perf_counter()-t0:.1f}s — {len(intermediary_lookup)} intermediaries")

    # reverse lookup: officer_id -> surname
    officer_to_surname = {}
    for surname, ids in officer_ids_by_surname.items():
        for nid in ids:
            officer_to_surname[nid] = surname

    # officer_id -> name
    officer_to_name = {}
    for surname, hits in officer_matches_by_surname.items():
        for r in hits:
            officer_to_name[r["node_id"]] = r["name"]

    print()
    print("--- Subgraph (entity neighbors only, by surname) ---")
    rows_for_step5 = []
    print(f"{'surname':<12} {'officer':<32} {'rel':<14} {'entity':<48} {'jurisdiction':<14} {'status':<10} {'sourceID'}")
    print("-" * 160)
    for s, e, rel, direction, start_date, src in edges:
        nbr_id = e if direction == "out" else s
        officer_id = s if direction == "out" else e
        surname = officer_to_surname.get(officer_id, "?")
        officer_name = officer_to_name.get(officer_id, "?")
        if nbr_id in entity_lookup:
            ent = entity_lookup[nbr_id]
            name = ent.get("name") or ent.get("original_name") or ""
            jurisdiction = ent.get("jurisdiction_description") or ent.get("jurisdiction") or ""
            status = ent.get("status") or ""
            sid = ent.get("sourceID") or ""
            print(f"{surname:<12} {officer_name[:30]:<32} {rel:<14} {name[:46]:<48} {jurisdiction[:12]:<14} {status[:8]:<10} {sid}")
            rows_for_step5.append((surname, officer_name, name, jurisdiction, status, sid))
    return rows_for_step5


def step5_compare(rows):
    print()
    print("=" * 70)
    print("STEP 5 — Compare to published Panama Papers findings (Rami Makhlouf)")
    print("=" * 70)
    found = {}
    extras = []
    for surname, officer, name, jur, status, sid in rows:
        matched = False
        for needle in PUBLISHED_COMPANIES:
            if needle.lower() in (name or "").lower():
                found.setdefault(needle, []).append((surname, officer, name, jur, status, sid))
                matched = True
        if not matched:
            extras.append((surname, officer, name, jur, status, sid))

    hit = 0
    for needle in PUBLISHED_COMPANIES:
        if needle in found:
            hit += 1
            for surname, officer, n, j, s, sid in found[needle]:
                print(f"  [HIT]  {needle:<24} -> {n} (via {officer} / {surname}, {j}, {s}, {sid})")
        else:
            print(f"  [MISS] {needle}")

    print()
    print(f"PASS/FAIL: {hit}/{len(PUBLISHED_COMPANIES)} published companies appear in 1-hop subgraph")
    print()
    print(f"Other entities in subgraph (candidate leads): {len(extras)}")
    # Group by jurisdiction for readability — offshore jurisdictions are the interesting ones
    offshore = [r for r in extras if any(j in r[3] for j in ["Virgin", "Panama", "Seychelles", "Jersey", "Cayman", "Bahamas", "Belize", "Niue", "Samoa"])]
    onshore = [r for r in extras if r not in offshore]
    print(f"  Offshore-jurisdiction extras ({len(offshore)}):")
    for surname, officer, n, j, s, sid in offshore[:30]:
        print(f"    [{surname:<10}] {n:<44}  ({j}, {s}, {sid})  via {officer}")
    if len(offshore) > 30:
        print(f"    ... +{len(offshore)-30} more")
    print(f"  Other-jurisdiction extras ({len(onshore)}):")
    for surname, officer, n, j, s, sid in onshore[:10]:
        print(f"    [{surname:<10}] {n:<44}  ({j}, {s}, {sid})  via {officer}")
    if len(onshore) > 10:
        print(f"    ... +{len(onshore)-10} more")


def main():
    overall_t0 = time.perf_counter()
    step1_pep()
    officer_matches = step2_officers()
    rows = step4_subgraph(officer_matches)
    step5_compare(rows)
    print()
    print(f"Total wall time: {time.perf_counter()-overall_t0:.1f}s")


if __name__ == "__main__":
    main()
