"""The import contract names agent packages one by one, so a new one must be added.

Scoping it to `app.agents.compiler` rather than `app.agents` is what lets the MCP
surface read `app.agents.tool_specs`; the cost is that a new agent package would not
be protected by accident, which this test turns into a failure.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

_AGENTS_DIR = Path(__file__).resolve().parents[1]
_PYPROJECT = _AGENTS_DIR.parents[1] / "pyproject.toml"

# Modules under app/agents/ that describe tools rather than run an agent, and so are
# deliberately importable by the other tool surface.
_SHARED_VOCABULARY = {"tool_specs"}


def find_agent_packages() -> set[str]:
    return {
        d.name
        for d in _AGENTS_DIR.iterdir()
        if d.is_dir() and (d / "__init__.py").exists() and not d.name.startswith("_")
    }


def find_protected_modules() -> set[str]:
    contracts = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    for contract in contracts["tool"]["importlinter"]["contracts"]:
        if contract["name"] == "an agent package is imported only by the entrypoint":
            return set(contract["protected_modules"])
    raise AssertionError("the agent-package contract is gone — this test guards nothing")


def test_every_agent_package_is_named_in_the_contract() -> None:
    protected = find_protected_modules()
    unprotected = [
        pkg for pkg in find_agent_packages() if f"app.agents.{pkg}" not in protected
    ]
    assert not unprotected, (
        "a new agent package is not covered by the import contract, so anything could "
        "import it. Add it to protected_modules in pyproject.toml (the contract is "
        f"scoped per-package so app.agents.tool_specs stays shared): {unprotected}"
    )


def test_shared_vocabulary_is_not_an_agent_package() -> None:
    """A module named here must not also be a package the contract should protect."""
    assert _SHARED_VOCABULARY.isdisjoint(find_agent_packages())
    for name in _SHARED_VOCABULARY:
        assert (_AGENTS_DIR / f"{name}.py").exists(), f"{name} no longer exists"
