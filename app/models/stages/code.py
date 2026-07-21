"""Validation for python-code stages: the inline code a python_row_function /
python_frame_function (or a publish stage's function block) carries must parse,
compile, and define the function the runtime calls. Split out of the `Stage`
model so the AST check lives beside other stage-type helpers rather than inline
on the model."""
from __future__ import annotations

import ast


def _binds_name(tree: ast.Module, name: str) -> bool:
    """True if a top-level def or assignment in `tree` binds `name` — i.e. what
    `exec`ing the code would expose for the runtime to look up and call."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return True
    return False


def validate_inline_function_code(code: str, function: str | None) -> None:
    """Raise ValueError if inline function `code` does not compile or does not
    define the function the runtime calls (`transform` by default, or `function`).

    A single stage's invariant, enforced at write time so broken code (e.g. a
    bare body with a top-level `return`) is rejected before the runner exec()s it."""
    try:
        tree = ast.parse(code)
        # compile() (like the runtime's exec) also catches a top-level `return`,
        # which ast.parse alone does not on 3.12.
        compile(code, "<inline function>", "exec")
    except SyntaxError as exc:
        raise ValueError(
            f"inline function code does not compile: {exc.msg} (line {exc.lineno}). "
            "Define a function, e.g. `def transform(row): ...; return row`."
        )
    wanted = function or "transform"
    if not (_binds_name(tree, wanted) or _binds_name(tree, "transform")):
        raise ValueError(
            f"inline function code must define `def {wanted}(...)` at the top level — "
            f"the runtime calls {wanted}(row) per row and expects a dict back"
        )
