"""Architecture: the methodology-editing agent reaches state through services, not disk.

app/compiler/agent is the domain agent — its tools edit a project's methodology.
Persistence is owned by app.services (and the loader beneath it), so these tools must
never open files directly. Enforcing that keeps a single disk owner and lets the tools
be tested without a filesystem. This is the content-level companion to the
import-boundary contracts in pyproject [tool.importlinter].
"""
from __future__ import annotations

from arch._helpers import (
    collect_called_funcs,
    collect_called_methods,
    iter_module_files,
    parse_module,
)

# Builtins and pathlib/os methods that read or write the filesystem.
_DISK_BUILTINS = {"open"}
_DISK_METHODS = {
    "read_text",
    "write_text",
    "read_bytes",
    "write_bytes",
    "unlink",
    "mkdir",
    "rmdir",
    "touch",
}


def test_compiler_agent_reaches_disk_only_through_services() -> None:
    offenders: list[str] = []
    for path in iter_module_files("compiler/agent"):
        tree = parse_module(path)
        hits = (collect_called_funcs(tree) & _DISK_BUILTINS) | (
            collect_called_methods(tree) & _DISK_METHODS
        )
        if hits:
            offenders.append(f"{path.name}: {sorted(hits)}")
    assert not offenders, (
        "app/compiler/agent must persist via app.services, not raw file I/O:\n  "
        + "\n  ".join(offenders)
    )
