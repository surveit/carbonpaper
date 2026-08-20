"""The arms a stage's code can take, and that code with each arm reporting itself."""
from __future__ import annotations

import ast
from dataclasses import dataclass

RECORDER_NAME = "record_branch"


@dataclass(frozen=True)
class BranchArm:
    # Built from tree position, so it survives a reformat that moves every line.
    id: str
    # Where to point a reader. Never the identity.
    line: int


@dataclass(frozen=True)
class _Site:
    arm: BranchArm
    insert_at: int
    indent: int


def find_branch_arms(source: str) -> list[BranchArm]:
    return [site.arm for site in _sites(source)]


def instrument_branches(source: str) -> tuple[str, list[BranchArm]]:
    """The same source with a recorder call opening each arm; valid python AND starlark."""
    sites = _sites(source)
    lines = source.split("\n")
    for site in sorted(sites, key=lambda s: s.insert_at, reverse=True):
        lines.insert(site.insert_at, " " * site.indent + f'{RECORDER_NAME}("{site.arm.id}")')
    return "\n".join(lines), [site.arm for site in sites]


def _sites(source: str) -> list[_Site]:
    found: list[_Site] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef):
            _walk_body(node.body, node.name, found)
    return sorted(found, key=lambda s: s.arm.line)


def _walk_body(body: list[ast.stmt], path: str, found: list[_Site]) -> None:
    for index, node in enumerate(body):
        if isinstance(node, ast.If):
            _walk_if(node, f"{path}/{index}", found)
        elif isinstance(node, ast.Try):
            _walk_try(node, f"{path}/{index}", found)


def _walk_if(node: ast.If, base: str, found: list[_Site], arm: str = "if") -> None:
    _open(node.body, f"{base}:{arm}", node.lineno, found)
    if not node.orelse:
        return
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        _walk_if(node.orelse[0], base, found, _next_elif(arm))
        return
    _open(node.orelse, f"{base}:else", node.lineno, found)


def _next_elif(arm: str) -> str:
    return "elif0" if arm == "if" else f"elif{int(arm.removeprefix('elif')) + 1}"


def _walk_try(node: ast.Try, base: str, found: list[_Site]) -> None:
    _open(node.body, f"{base}:try", node.lineno, found)
    for position, handler in enumerate(node.handlers):
        _open(handler.body, f"{base}:except{position}", handler.lineno, found)
    if node.orelse:
        _open(node.orelse, f"{base}:else", node.lineno, found)


def _open(body: list[ast.stmt], arm_id: str, header_line: int, found: list[_Site]) -> None:
    first = body[0]
    # Nowhere to insert, so the arm is not offered at all.
    if first.lineno == header_line:
        return
    found.append(_Site(BranchArm(arm_id, first.lineno), first.lineno - 1, first.col_offset))
    _walk_body(body, arm_id, found)
