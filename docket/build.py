import argparse
import sqlite3
from pathlib import Path

from docket.federal_register import (
    FederalRegisterDocument,
    fetch_docket_documents,
    fetch_document_xml,
)
from docket.warehouse_load import load_rule_documents
from docket.warehouse_schema import PROVISION_HISTORY_VIEW, SCHEMA


def main() -> None:
    options = _parse_arguments()
    proposed, final = _pick_rule_pair(options.docket)
    connection = sqlite3.connect(options.out)
    connection.executescript(SCHEMA)
    _store_rule(connection, options.docket, proposed, final)
    counts = load_rule_documents(
        connection,
        _rule_id(options.docket),
        fetch_document_xml(proposed, options.cache),
        fetch_document_xml(final, options.cache),
    )
    connection.executescript(PROVISION_HISTORY_VIEW)
    connection.commit()
    _report(connection, options.docket, counts)


def _pick_rule_pair(
    docket: str,
) -> tuple[FederalRegisterDocument, FederalRegisterDocument]:
    """The longest document of each type: a docket also carries corrections and hearing notices."""
    documents = fetch_docket_documents(docket)
    proposed = _longest(documents, "Proposed Rule", docket)
    final = _longest(documents, "Rule", docket)
    return proposed, final


def _longest(
    documents: list[FederalRegisterDocument], document_type: str, docket: str
) -> FederalRegisterDocument:
    matching = [d for d in documents if d.type == document_type and d.full_text_xml_url]
    if not matching:
        raise LookupError(
            f"docket {docket} has no '{document_type}' with retrievable XML - a rule pair "
            "needs both halves, so this docket cannot be loaded"
        )
    return max(matching, key=lambda d: d.page_length or 0)


def _store_rule(
    connection: sqlite3.Connection,
    docket: str,
    proposed: FederalRegisterDocument,
    final: FederalRegisterDocument,
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _rule_id(docket),
            next(iter(final.regulation_id_numbers), None),
            docket,
            docket.split("-")[0],
            final.title,
            proposed.document_number,
            final.document_number,
            proposed.publication_date,
            final.publication_date,
        ),
    )


def _rule_id(docket: str) -> str:
    return docket


def _report(
    connection: sqlite3.Connection, docket: str, counts: dict[str, int]
) -> None:
    print(f"docket {docket}")
    for key in sorted(counts):
        print(f"  parsed {key:<22} {counts[key]:>6,}")
    print()
    for table in ("rules", "provisions", "provision_versions", "amendments"):
        stored = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  stored {table:<22} {stored:>6,}")
    _check_pair_is_comparable(connection, _rule_id(docket))


def _check_pair_is_comparable(connection: sqlite3.Connection, rule_id: str) -> None:
    """A proposal with no regulatory text cannot be diffed; say so, not an empty join."""
    counts = dict(
        connection.execute(
            "SELECT doc_type, COUNT(*) FROM provision_versions WHERE rule_id = ? GROUP BY doc_type",
            (rule_id,),
        ).fetchall()
    )
    print()
    if not counts.get("proposed"):
        print(
            "  WARNING: the proposal published no regulatory text, so proposal-to-final"
        )
        print("           comparison is impossible for this docket.")
        return
    joined = connection.execute(
        "SELECT COUNT(*) FROM provision_versions a JOIN provision_versions b"
        " ON a.provision_id = b.provision_id AND a.rule_id = b.rule_id"
        " WHERE a.rule_id = ? AND a.doc_type = 'proposed' AND b.doc_type = 'final'",
        (rule_id,),
    ).fetchone()[0]
    print(f"  provisions present in BOTH documents (joinable): {joined}")


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a provision-level warehouse for one rulemaking docket."
    )
    parser.add_argument("--docket", required=True, help="e.g. EPA-HQ-OPPT-2022-0902")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--cache", type=Path, default=None, help="directory to cache fetched XML"
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
