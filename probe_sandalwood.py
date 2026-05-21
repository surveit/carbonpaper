"""
Quick probe — why is Sandalwood Continental missing from Roldugin's 1-hop subgraph?

If Sandalwood exists in the data but is 2+ hops from Roldugin, that tells us the
prototype needs multi-hop traversal as a first-class capability.
"""

import csv
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

csv.field_size_limit(10_000_000)

DOWNLOADS = Path(r"C:\Users\shuha\Downloads")
ENTITIES_CSV = DOWNLOADS / "nodes-entities.csv"
RELS_CSV = DOWNLOADS / "relationships.csv"
OFFICERS_CSV = DOWNLOADS / "nodes-officers.csv"
INTERMEDIARIES_CSV = DOWNLOADS / "nodes-intermediaries.csv"

ROLDUGIN_OFFICER_IDS = {"12079386", "12096275", "12180773", "80061955"}


def main():
    # Step A — does Sandalwood Continental exist in entities at all?
    print("Step A: search nodes-entities.csv for 'sandalwood continental'")
    sandalwood_ids = set()
    with ENTITIES_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").lower()
            if "sandalwood continental" in name:
                sandalwood_ids.add(row["node_id"])
                print(f"  FOUND: {row['node_id']}  {row['name']}  ({row.get('jurisdiction_description')}, {row.get('status')}, {row.get('sourceID')})")
    if not sandalwood_ids:
        print("  Sandalwood Continental NOT in dataset.")
        return

    # Step B — pull all edges touching Sandalwood, see what's at the other end
    print()
    print("Step B: relationships touching Sandalwood")
    sandalwood_neighbor_ids = set()
    sandalwood_edges = []
    with RELS_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            s, e = row["node_id_start"], row["node_id_end"]
            if s in sandalwood_ids:
                sandalwood_edges.append(row)
                sandalwood_neighbor_ids.add(e)
            elif e in sandalwood_ids:
                sandalwood_edges.append(row)
                sandalwood_neighbor_ids.add(s)
    print(f"  edges: {len(sandalwood_edges)}, distinct neighbors: {len(sandalwood_neighbor_ids)}")
    for r in sandalwood_edges:
        print(f"    {r['node_id_start']} --{r['rel_type']}--> {r['node_id_end']}  ({r['start_date']}..{r['end_date']}, {r['sourceID']})")

    # Step C — are any of Sandalwood's neighbors also a Roldugin neighbor?
    # Compute Roldugin's 1-hop neighbor set fresh.
    print()
    print("Step C: compute 1-hop neighbors of Roldugin officer ids")
    roldugin_neighbor_ids = set()
    with RELS_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            s, e = row["node_id_start"], row["node_id_end"]
            if s in ROLDUGIN_OFFICER_IDS:
                roldugin_neighbor_ids.add(e)
            elif e in ROLDUGIN_OFFICER_IDS:
                roldugin_neighbor_ids.add(s)
    overlap = sandalwood_neighbor_ids & roldugin_neighbor_ids
    print(f"  Roldugin 1-hop neighbors: {len(roldugin_neighbor_ids)}")
    print(f"  Overlap with Sandalwood neighbors: {len(overlap)}")
    if overlap:
        print(f"  → Sandalwood is 2 hops from Roldugin via these node_ids: {overlap}")
        # Identify those nodes
        print()
        print("Step D: identify the 2-hop bridge nodes")
        for cls_name, csv_file in [
            ("Officer", OFFICERS_CSV),
            ("Entity", ENTITIES_CSV),
            ("Intermediary", INTERMEDIARIES_CSV),
        ]:
            with csv_file.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row["node_id"] in overlap:
                        print(f"    [{cls_name}] {row['node_id']}: {row.get('name')}  ({row.get('countries') or row.get('jurisdiction_description', '')}, {row.get('sourceID')})")
    else:
        print("  No 2-hop connection. Sandalwood is 3+ hops from Roldugin (or in a disjoint component).")


if __name__ == "__main__":
    main()
