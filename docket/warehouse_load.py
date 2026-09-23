import hashlib
import re
import sqlite3
from pathlib import Path

from docket.citations import Citation
from docket.regtext import parse_regtext

_LEGAL_SUFFIX = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|lp|corp|corporation|company|co|plc|et al)\b\.?",
    re.IGNORECASE,
)
_PARENS = re.compile(r"\([^)]*\)")

INDUSTRY_HINTS = (
    "manufacturer",
    "petroleum",
    "trucking",
    "automotive",
    "daimler",
    "volvo",
    "toyota",
    "stellantis",
    "mining",
    "fuels",
    "ryder",
    "chamber of commerce",
    "enterprise institute",
    "energy research",
    "api",
    "emission controls",
)
ADVOCACY_HINTS = (
    "environmental",
    "climate",
    "health",
    "lung",
    "nurses",
    "physicians",
    "conservation",
    "clean air",
    "moms",
    "concerned scientists",
    "parks",
    "ministries",
    "evangelical",
    "defense fund",
    "council",
    "alliance of nurses",
    "public health",
)


def provision_id(citation: Citation) -> str:
    return citation.key()


def upsert_provision(connection: sqlite3.Connection, citation: Citation) -> str:
    identifier = provision_id(citation)
    connection.execute(
        "INSERT OR IGNORE INTO provisions VALUES (?, ?, ?, ?)",
        (identifier, citation.cfr_title, citation.cfr_part, citation.section),
    )
    return identifier


def resolve_org(connection: sqlite3.Connection, raw_name: str) -> str:
    """Aliases collapse on a normalised name so 'AJW Inc.' and 'AJW, Inc.' are one organisation."""
    alias = raw_name.strip()
    existing = connection.execute(
        "SELECT org_id FROM org_aliases WHERE alias = ?", (alias,)
    ).fetchone()
    if existing:
        return existing[0]
    canonical = _canonical_name(alias)
    identifier = hashlib.sha1(canonical.encode()).hexdigest()[:12]
    connection.execute(
        "INSERT OR IGNORE INTO orgs VALUES (?, ?, ?)",
        (identifier, canonical, classify_sector(canonical)),
    )
    connection.execute(
        "INSERT OR REPLACE INTO org_aliases VALUES (?, ?)", (alias, identifier)
    )
    return identifier


def classify_sector(name: str) -> str:
    lowered = name.lower()
    if any(hint in lowered for hint in INDUSTRY_HINTS):
        return "industry"
    if any(hint in lowered for hint in ADVOCACY_HINTS):
        return "advocacy"
    if lowered in ("epa", "omb", "oira", "dot", "doe", "nhtsa"):
        return "government"
    return "unknown"


def load_rule_documents(
    connection: sqlite3.Connection, rule_id: str, proposed: Path, final: Path
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for doc_type, path in (("proposed", proposed), ("final", final)):
        provisions, amendments = parse_regtext(path)
        for parsed in provisions:
            identifier = upsert_provision(connection, parsed.citation)
            connection.execute(
                "INSERT OR REPLACE INTO provision_versions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rule_id,
                    doc_type,
                    identifier,
                    parsed.heading,
                    parsed.body,
                    len(parsed.body),
                ),
            )
        for amendment in amendments:
            identifier = upsert_provision(connection, amendment.citation)
            connection.execute(
                "INSERT OR REPLACE INTO amendments VALUES (?, ?, ?, ?, ?)",
                (rule_id, doc_type, identifier, amendment.verb, amendment.instruction),
            )
        counts[f"{doc_type}_provisions"] = len(provisions)
        counts[f"{doc_type}_amendments"] = len(amendments)
    return counts


def _canonical_name(raw: str) -> str:
    without_parens = _PARENS.sub(" ", raw)
    without_suffix = _LEGAL_SUFFIX.sub(" ", without_parens)
    cleaned = re.sub(r"[^A-Za-z0-9 &]", " ", without_suffix)
    return re.sub(r"\s+", " ", cleaned).strip().title()
