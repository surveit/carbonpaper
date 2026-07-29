"""Validation for python-code stages: the inline code a python_row_function /
python_frame_function (or a publish stage's function block) carries must parse,
compile, and define the function the runtime calls. Also holds the wording of the
`summary` every authored-code handle asks for, so PythonFunction and FilterConfig
cannot drift apart."""
from __future__ import annotations

import ast

# The instruction an authoring client reads when it fills in `summary`. Python
# code is the one handle a non-engineer reviewer cannot read for themselves, so
# the summary — not the code — is what the stage page leads with; it is written
# alongside the code, from the methodology, and says the RULE rather than the
# implementation.
SUMMARY_DESCRIPTION = (
    "REQUIRED in practice: one or two plain sentences telling a non-engineer what this "
    "step does, written from the methodology at the same time as the code. State the "
    "rule and its intent, not the implementation — \"marks a bill withdrawn when its "
    "status text says so, leaving the score blank\", never \"applies a regex to `status` "
    "and returns a dict\". No Python vocabulary (function, dict, DataFrame, None, "
    "regex); the only identifiers to use are column names the reader already sees in "
    "the schema. Anything conditional or surprising about the behaviour — rows left "
    "untouched, values deliberately blanked — belongs here, because it is what a "
    "reviewer would otherwise have to read the code to find. Rewrite it whenever the "
    "code changes."
)


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


def validate_inline_function_code(
    code: str,
    function: str | None,
    default_name: str = "transform",
    return_hint: str = "a dict",
) -> None:
    """Raise ValueError if inline function `code` does not compile or does not
    define the function the runtime calls (`default_name` unless `function`
    names another).

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
            f"Define a function, e.g. `def {default_name}(row): ...; return row`."
        )
    wanted = function or default_name
    if not (_binds_name(tree, wanted) or _binds_name(tree, default_name)):
        raise ValueError(
            f"inline function code must define `def {wanted}(...)` at the top level — "
            f"the runtime calls {wanted}(row) per row and expects {return_hint} back"
        )
