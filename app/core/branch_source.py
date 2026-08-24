"""The branches a stage's code can take, and that code with each one reporting itself."""
from __future__ import annotations

import ast
from dataclasses import dataclass

RECORDER_NAME = "record_branch"
_INDENT = 4

# So "which arm did this row take" has no one answer inside them. See docs/branch-analysis.md
_NOT_ONCE_PER_ROW = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


@dataclass(frozen=True)
class Branch:
    # Built from tree position, so it survives a reformat that moves every line.
    id: str
    # Where the branch's body starts. Where to point a reader, never the identity.
    line: int
    column: int
    # The body's last line, so a reader lights the block rather than its first statement.
    end_line: int = 0


@dataclass(frozen=True, kw_only=True)
class ChoiceBranch(Branch):
    """One arm of `x if c else y`: a value with no suite, so the recorder wraps the value."""

    end_column: int
    # No line opens the arm, so how it reads has to be carried rather than found in the source.
    label: str


def find_branches(source: str) -> list[Branch]:
    return _order_for_reading(_branches(ast.parse(source)))


def read_branch_test(lines: list[str], branch: Branch) -> tuple[int, str]:
    """Which line to point a reader at for this branch, and how the branch reads there."""
    if isinstance(branch, ChoiceBranch):
        return branch.line, branch.label
    prefix = lines[branch.line - 1][:branch.column]
    if prefix.strip():
        return branch.line, prefix.strip()  # `if x: y = 1`
    last = branch.line - 2
    while last > 0 and not lines[last].strip():
        last -= 1
    first = last
    while first > 0 and not _opens_a_branch(lines[first]):
        first -= 1
    if not _opens_a_branch(lines[first]):
        return last + 1, lines[last].strip()
    return first + 1, " ".join(line.strip() for line in lines[first:last + 1])


_OPENERS = ("if", "elif", "else", "try", "except")


def _opens_a_branch(line: str) -> bool:
    head = line.strip().split("(")[0].split(":")[0].split()
    return bool(head) and head[0] in _OPENERS


def instrument_branches(source: str) -> tuple[str, list[Branch]]:
    """The same source with a recorder call opening each branch; valid python AND starlark."""
    lines = source.split("\n")
    branches = _branches(ast.parse(source))
    for (line, column), text in sorted(_place_recorders(branches, lines).items(), reverse=True):
        at = line - 1
        lines[at] = lines[at][:column] + text + lines[at][column:]
    return "\n".join(lines), _order_for_reading(branches)


def _order_for_reading(branches: list[Branch]) -> list[Branch]:
    return sorted(branches, key=lambda b: (b.line, b.column, b.id))


def _place_recorders(branches: list[Branch], lines: list[str]) -> dict[tuple[int, int], str]:
    """What to splice in, keyed by where. Branches arrive outermost first, so wrappers nest."""
    edits: dict[tuple[int, int], str] = {}
    for branch in branches:
        if isinstance(branch, ChoiceBranch):
            closes = (branch.end_line, branch.end_column)
            edits[closes] = "))" + edits.get(closes, "")
            opens = f'({RECORDER_NAME}("{branch.id}") or ('
        else:
            opens = _write_suite_call(lines[branch.line - 1], branch)
        at = (branch.line, branch.column)
        edits[at] = edits.get(at, "") + opens
    return edits


def _write_suite_call(line: str, branch: Branch) -> str:
    header = line[:branch.column]
    pad = " " * _indent_for(header, branch)
    # `if x: y = 1` — the header keeps its line, the body moves down under the call.
    lead = "" if not header.strip() else "\n" + pad
    return f'{lead}{RECORDER_NAME}("{branch.id}")\n{pad}'


def _indent_for(header: str, branch: Branch) -> int:
    if not header.strip():
        return branch.column
    return len(header) - len(header.lstrip()) + _INDENT


def _branches(tree: ast.AST) -> list[Branch]:
    found: list[Branch] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            _walk_body(node.body, node.name, found)
    return found


def _walk_body(body: list[ast.stmt], path: str, found: list[Branch]) -> None:
    for index, node in enumerate(body):
        _walk_choices(node, f"{path}/{index}", found)
        if isinstance(node, ast.If):
            _walk_if(node, f"{path}/{index}", found)
        elif isinstance(node, ast.Try):
            _walk_try(node, f"{path}/{index}", found)


def _walk_if(node: ast.If, base: str, found: list[Branch], kind: str = "if") -> None:
    _open(node.body, f"{base}:{kind}", found)
    if not node.orelse:
        return
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        _walk_if(node.orelse[0], base, found, _next_elif(kind))
        return
    _open(node.orelse, f"{base}:else", found)


def _next_elif(kind: str) -> str:
    return "elif0" if kind == "if" else f"elif{int(kind.removeprefix('elif')) + 1}"


def _walk_try(node: ast.Try, base: str, found: list[Branch]) -> None:
    _open(node.body, f"{base}:try", found)
    for position, handler in enumerate(node.handlers):
        _open(handler.body, f"{base}:except{position}", found)
    if node.orelse:
        _open(node.orelse, f"{base}:else", found)


def _open(body: list[ast.stmt], branch_id: str, found: list[Branch]) -> None:
    first = body[0]
    found.append(Branch(branch_id, first.lineno, first.col_offset,
                        body[-1].end_lineno or first.lineno))
    _walk_body(body, branch_id, found)


def _walk_choices(node: ast.stmt, base: str, found: list[Branch]) -> None:
    # A def's decorators and defaults run where it is written; its body is walked apart.
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    for order, chain in enumerate(_find_choices(node)):
        _open_arms(chain, f"{base}:choice{order}", found)


def _find_choices(node: ast.stmt) -> list[ast.IfExp]:
    """Every `x if c else y` the statement itself evaluates, each chain once, outermost first."""
    found: list[ast.IfExp] = []
    for child in ast.iter_child_nodes(node):
        _scan_choices(child, found)
    return found


def _scan_choices(node: ast.AST, found: list[ast.IfExp]) -> None:
    if isinstance(node, (ast.stmt, *_NOT_ONCE_PER_ROW)):
        return
    if not isinstance(node, ast.IfExp):
        for child in ast.iter_child_nodes(node):
            _scan_choices(child, found)
        return
    found.append(node)
    for part in _read_chain_parts(node):
        _scan_choices(part, found)


def _read_chain_parts(chain: ast.IfExp) -> list[ast.expr]:
    """`a if p else b if q else c` is one chain: both its tests and all three of its arms."""
    parts: list[ast.expr] = []
    node = chain
    while True:
        parts.extend((node.test, node.body))
        if not isinstance(node.orelse, ast.IfExp):
            parts.append(node.orelse)
            return parts
        node = node.orelse


def _open_arms(chain: ast.IfExp, base: str, found: list[Branch]) -> None:
    node, kind, word = chain, "if", "if"
    while True:
        found.append(_read_arm(node.body, f"{base}:{kind}", f"{word} {ast.unparse(node.test)}"))
        if not isinstance(node.orelse, ast.IfExp):
            found.append(_read_arm(node.orelse, f"{base}:else", "else"))
            return
        node, kind, word = node.orelse, _next_elif(kind), "elif"


def _read_arm(value: ast.expr, branch_id: str, label: str) -> ChoiceBranch:
    end_line, end_column = value.end_lineno, value.end_col_offset
    if end_line is None or end_column is None:
        raise ValueError(f"branch {branch_id} has an arm the parser gave no end position")
    return ChoiceBranch(branch_id, value.lineno, value.col_offset, end_line,
                        end_column=end_column, label=label)
