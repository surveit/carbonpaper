"""Architecture: an import-linter contract states who MAY import, never who may not.

A `forbidden` contract is allowed-until-listed — a package added tomorrow is legal
because nobody remembered to name it. `protected` and `layers` are denied-until-listed,
so the same package is caught. The ledger below is a ratchet: it may shrink, never grow.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

_WHITELIST_TYPES = frozenset({"protected", "layers"})

# The one contract still expressed as a denial, and what it is waiting on. It carves an
# exception out of the app.services whitelist, which admits app.runtime wholesale; saying
# it positively means naming the runtime submodules that may reach a service, which is a
# change to the runtime boundary rather than to this file. Remove the entry, do not add one.
_BLACKLIST_LEDGER = frozenset({
    "the runner reads no services module — a caller hands it a version's stages",
})


def read_contracts() -> list[dict]:
    config = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["importlinter"]["contracts"]


def find_blacklist_offenders(contracts: list[dict]) -> list[str]:
    return [
        f"{c['name']!r} (type={c['type']})"
        for c in contracts
        if c["type"] not in _WHITELIST_TYPES and c["name"] not in _BLACKLIST_LEDGER
    ]


def find_stale_ledger_entries(contracts: list[dict]) -> list[str]:
    live = {c["name"] for c in contracts if c["type"] not in _WHITELIST_TYPES}
    return sorted(_BLACKLIST_LEDGER - live)


def test_every_contract_says_who_may_import() -> None:
    offenders = find_blacklist_offenders(read_contracts())
    assert not offenders, (
        "an import-linter contract must say who MAY import a module (`protected`) or "
        "where it sits (`layers`) — a `forbidden` list is legal for anything nobody "
        "thought to name:\n  " + "\n  ".join(offenders)
    )


def test_the_blacklist_ledger_holds_no_dead_entries() -> None:
    stale = find_stale_ledger_entries(read_contracts())
    assert not stale, (
        "these contracts were converted or deleted, so the ledger in this file must "
        "drop them — it is a ratchet:\n  " + "\n  ".join(stale)
    )
