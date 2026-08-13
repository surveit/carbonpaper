"""Inlining the source of a stage whose `function` named an importable module.

Shared by alembic revision 0013 and its test, so the resolution rule — a dotted
path whose leading segment names the projects root — is stated once.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models.stages.code import validate_inline_function_code


class ModuleSourceUnreadable(ValueError):
    """The source a stage pointed at cannot be read; an empty `code` is never written instead."""


def inline_module_function(spec: dict[str, Any], projects_root: Path) -> bool:
    """Rewrite one stage spec from `kind: module` to `kind: inline`; False if unchanged."""
    function = spec.get("function")
    if not isinstance(function, dict) or function.get("kind") != "module":
        return False
    stage_id = spec.get("id", "?")
    _refuse_competing_code(function, stage_id)
    code = read_module_source(function.get("module"), stage_id, projects_root)
    # The stored spec must load after the rewrite, and PythonFunction runs this on
    # every inline block: a module whose source binds no `transform` is a refusal.
    validate_inline_function_code(code, function.get("function"))
    function["kind"] = "inline"
    function["code"] = code
    function.pop("module")
    return True


def read_module_source(module: Any, stage_id: Any, projects_root: Path) -> str:
    if not isinstance(module, str) or not module:
        raise ModuleSourceUnreadable(
            f"stage {stage_id!r}: function.kind=module carries no `module` to read"
        )
    path = resolve_module_path(module, stage_id, projects_root)
    if not path.is_file():
        raise ModuleSourceUnreadable(
            f"stage {stage_id!r}: module `{module}` has no source file at {path}"
        )
    source = path.read_text(encoding="utf-8")
    if not source.strip():
        raise ModuleSourceUnreadable(
            f"stage {stage_id!r}: module `{module}` at {path} holds no code"
        )
    return source


def resolve_module_path(module: str, stage_id: Any, projects_root: Path) -> Path:
    """`<root>.<project>.code.x` → `<projects_root>/<project>/code/x.py`."""
    head, _, tail = module.partition(".")
    if head != projects_root.name or not tail:
        raise ModuleSourceUnreadable(
            f"stage {stage_id!r}: module `{module}` does not begin with the projects "
            f"root `{projects_root.name}`, so its source is not locatable from here"
        )
    return projects_root / Path(*tail.split(".")).with_suffix(".py")


def _refuse_competing_code(function: dict[str, Any], stage_id: Any) -> None:
    """Overwriting authored inline code with a module's would lose whichever one ran."""
    if (function.get("code") or "").strip():
        raise ModuleSourceUnreadable(
            f"stage {stage_id!r}: carries both `module` and inline `code` — a human "
            "must say which one the stage ran before either can be kept"
        )
