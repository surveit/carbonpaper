"""Architecture: the MCP server is a TRANSPORT, so app.tools is its only way into the app.
A body or a schema written here is a tool the editing agent cannot have — which is how
run_workflow sat on one surface for so long. Not an import-linter contract: saying it
there takes a `forbidden` list, which is allowed-until-named
(tests/arch/test_contracts_are_whitelists.py), and app.models is open to everything."""
from __future__ import annotations

import ast
from pathlib import Path

_MCP = Path(__file__).resolve().parents[2] / "app" / "mcp"
_ALLOWED = ("app.tools", "app.mcp")


def find_app_imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return {name for name in names if name == "app" or name.startswith("app.")}


def find_illegal_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted(
        name for name in find_app_imports(tree)
        if not any(name == ok or name.startswith(f"{ok}.") for ok in _ALLOWED)
    )


def test_the_mcp_server_imports_nothing_but_app_tools() -> None:
    offenders = {
        path.name: illegal
        for path in sorted(_MCP.rglob("*.py"))
        if (illegal := find_illegal_imports(path))
    }
    assert not offenders, (
        "app/mcp may reach the app only through app.tools — move the body, the schema "
        "or the type behind a tool in app/tools/ and import it from there, so both "
        f"authoring surfaces get it: {offenders}"
    )


def test_the_scan_sees_the_imports_the_server_does_carry() -> None:
    # Without this the test above passes on an unparsed or empty directory.
    found = find_app_imports(ast.parse((_MCP / "server.py").read_text(encoding="utf-8")))
    assert {"app.tools", "app.tools.tool_specs"} <= found
