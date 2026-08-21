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


def find_branches(source: str) -> list[Branch]:
    return _branches(ast.parse(source))


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
    found.append(Branch(branch_id, first.lineno, first.col_offset))
    _walk_body(body, branch_id, found)
