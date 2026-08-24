"""The branches a stage's code can take, and that code with each one reporting itself."""
from __future__ import annotations

import ast
from dataclasses import dataclass

RECORDER_NAME = "record_branch"
_INDENT = 4


@dataclass(frozen=True)
class Branch:
    # Built from tree position, so it survives a reformat that moves every line.
    id: str
    # Where the branch's body starts. Where to point a reader, never the identity.
    line: int
    column: int
    # The body's last line, so a reader lights the block rather than its first statement.
    end_line: int = 0


def find_branches(source: str) -> list[Branch]:
    return _branches(ast.parse(source))


def read_branch_test(lines: list[str], branch: Branch) -> tuple[int, str]:
    """`Branch.line` is the body's first statement; the test the row passed is above it."""
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
    for branch in sorted(branches, key=lambda b: b.line, reverse=True):
        at = branch.line - 1
        lines[at:at + 1] = _with_recorder(lines[at], branch)
    return "\n".join(lines), branches


def _with_recorder(line: str, branch: Branch) -> list[str]:
    header = line[:branch.column]
    call = " " * _indent_for(header, branch) + f'{RECORDER_NAME}("{branch.id}")'
    if not header.strip():
        return [call, line]
    # `if x: y = 1` — the header keeps its line, the body moves down under the call.
    return [header.rstrip(), call,
            " " * _indent_for(header, branch) + line[branch.column:].lstrip()]


def _indent_for(header: str, branch: Branch) -> int:
    if not header.strip():
        return branch.column
    return len(header) - len(header.lstrip()) + _INDENT


def _branches(tree: ast.AST) -> list[Branch]:
    found: list[Branch] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            _walk_body(node.body, node.name, found)
    return sorted(found, key=lambda b: (b.line, b.id))


def _walk_body(body: list[ast.stmt], path: str, found: list[Branch]) -> None:
    for index, node in enumerate(body):
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
