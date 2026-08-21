"""The arms a stage's code can take, and that code with each arm reporting itself."""
from __future__ import annotations

import ast
from dataclasses import dataclass

RECORDER_NAME = "record_branch"
_INDENT = 4


@dataclass(frozen=True)
class BranchArm:
    # Built from tree position, so it survives a reformat that moves every line.
    id: str
    # Where to point a reader. Never the identity.
    line: int


@dataclass(frozen=True)
class _Opening:
    arm: BranchArm
    at_line: int
    indent: int
    # Set where the arm's body shares its header's line, so the line is split first.
    split_column: int | None


def find_branch_arms(source: str) -> list[BranchArm]:
    return [opening.arm for opening in _openings(source)]


def instrument_branches(source: str) -> tuple[str, list[BranchArm]]:
    """The same source with a recorder call opening each arm; valid python AND starlark."""
    openings = _openings(source)
    lines = source.split("\n")
    for opening in sorted(openings, key=lambda o: o.at_line, reverse=True):
        lines[opening.at_line:opening.at_line + 1] = _opened(lines[opening.at_line], opening)
    return "\n".join(lines), [opening.arm for opening in openings]


def _opened(line: str, opening: _Opening) -> list[str]:
    call = " " * opening.indent + f'{RECORDER_NAME}("{opening.arm.id}")'
    if opening.split_column is None:
        return [call, line]
    # `if x: y = 1` — the header keeps its line, the body moves down under the call.
    return [line[:opening.split_column].rstrip(), call,
            " " * opening.indent + line[opening.split_column:].lstrip()]


def _openings(source: str) -> list[_Opening]:
    lines = source.split("\n")
    found: list[_Opening] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef):
            _walk_body(node.body, node.name, lines, found)
    return sorted(found, key=lambda o: (o.arm.line, o.arm.id))


def _walk_body(
    body: list[ast.stmt], path: str, lines: list[str], found: list[_Opening]
) -> None:
    for index, node in enumerate(body):
        if isinstance(node, ast.If):
            _walk_if(node, f"{path}/{index}", lines, found)
        elif isinstance(node, ast.Try):
            _walk_try(node, f"{path}/{index}", lines, found)


def _walk_if(
    node: ast.If, base: str, lines: list[str], found: list[_Opening], arm: str = "if"
) -> None:
    _open(node.body, f"{base}:{arm}", lines, found)
    if not node.orelse:
        return
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        _walk_if(node.orelse[0], base, lines, found, _next_elif(arm))
        return
    _open(node.orelse, f"{base}:else", lines, found)


def _next_elif(arm: str) -> str:
    return "elif0" if arm == "if" else f"elif{int(arm.removeprefix('elif')) + 1}"


def _walk_try(node: ast.Try, base: str, lines: list[str], found: list[_Opening]) -> None:
    _open(node.body, f"{base}:try", lines, found)
    for position, handler in enumerate(node.handlers):
        _open(handler.body, f"{base}:except{position}", lines, found)
    if node.orelse:
        _open(node.orelse, f"{base}:else", lines, found)


def _open(
    body: list[ast.stmt], arm_id: str, lines: list[str], found: list[_Opening]
) -> None:
    first = body[0]
    line = lines[first.lineno - 1]
    header = line[:first.col_offset]
    # Anything but whitespace before the body means it shares its header's line.
    shares_header_line = bool(header.strip())
    found.append(_Opening(
        arm=BranchArm(arm_id, first.lineno),
        at_line=first.lineno - 1,
        indent=(len(header) - len(header.lstrip()) + _INDENT) if shares_header_line
        else first.col_offset,
        split_column=first.col_offset if shares_header_line else None,
    ))
    _walk_body(body, arm_id, lines, found)
