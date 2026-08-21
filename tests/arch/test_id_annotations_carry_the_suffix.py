"""Architecture: anything annotated `ID` is named `id`, `*_id`, or their plurals."""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_ALIAS = "ID"

# Empty and staying empty: an exemption lets a name deny what its annotation says.
_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()


def find_misnamed_id_annotations(tree: ast.Module) -> list[tuple[int, str, str]]:
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            offenders += _check_signature(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            offenders += _check_one(node.annotation, "field", node.target.id, node.lineno)
    return sorted(offenders)


def reads_as_an_id(name: str) -> bool:
    stem = name[:-1] if name.endswith("s") else name
    return stem == "id" or stem.endswith("_id")


def names_the_alias(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    return any(
        (isinstance(node, ast.Name) and node.id == _ALIAS)
        or (isinstance(node, ast.Attribute) and node.attr == _ALIAS)
        for node in ast.walk(annotation)
    )


def find_id_suffix_offenders(paths: list[Path], repo_root: Path) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(repo_root).as_posix()
        for lineno, kind, name in find_misnamed_id_annotations(parse_module(path)):
            if (relative, name) not in _ALLOWLIST:
                offenders.append(f"{relative}:{lineno}  {kind} {name}")
    return offenders


def test_every_name_annotated_id_reads_as_an_id() -> None:
    offenders = find_id_suffix_offenders(find_source_files_under(_APP_ROOT), _REPO_ROOT)
    assert not offenders, (
        f"`{_ALIAS}` is an alias for `str`, so the NAME is the only thing telling a "
        "reader an identity from a title, a slug or a sentence — rename it to end in "
        "`_id` (or `_ids`), or annotate it `str` because it is not an identity:\n  "
        + "\n  ".join(offenders)
    )


def test_the_alias_is_declared_where_this_rule_expects_it() -> None:
    tree = parse_module(_APP_ROOT / "core" / "ids.py")
    declared = {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert _ALIAS in declared, (
        f"app/core/ids.py no longer declares `{_ALIAS}` — this rule reads that name out "
        "of every annotation under app/, so a move leaves it matching nothing and "
        "passing vacuously. Point `_ALIAS` at the new declaration."
    )


def _check_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, str, str]]:
    args = node.args
    named = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    named += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
    offenders = [
        offender
        for arg in named
        for offender in _check_one(arg.annotation, "parameter", arg.arg, arg.lineno)
    ]
    return offenders + _check_one(node.returns, "return of", node.name, node.lineno)


def _check_one(
    annotation: ast.expr | None, kind: str, name: str, lineno: int
) -> list[tuple[int, str, str]]:
    if names_the_alias(annotation) and not reads_as_an_id(name):
        return [(lineno, kind, name)]
    return []


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_reads_as_an_id_accepts_the_bare_word() -> None:
    assert reads_as_an_id("id") is True


def test_reads_as_an_id_accepts_a_suffixed_name() -> None:
    assert reads_as_an_id("project_id") is True


def test_reads_as_an_id_accepts_a_plural() -> None:
    assert reads_as_an_id("stage_ids") is True


def test_reads_as_an_id_rejects_a_name_that_merely_ends_in_the_letters() -> None:
    assert reads_as_an_id("valid") is False
    assert reads_as_an_id("uuid") is False


def test_reads_as_an_id_rejects_a_bare_noun() -> None:
    assert reads_as_an_id("project") is False


def test_names_the_alias_reads_through_a_container() -> None:
    assert names_the_alias(ast.parse("list[ID]", mode="eval").body) is True


def test_names_the_alias_reads_through_a_union() -> None:
    assert names_the_alias(ast.parse("ID | None", mode="eval").body) is True


def test_names_the_alias_reads_a_dotted_reference() -> None:
    assert names_the_alias(ast.parse("ids.ID", mode="eval").body) is True


def test_names_the_alias_ignores_a_name_that_merely_contains_it() -> None:
    assert names_the_alias(ast.parse("UUID", mode="eval").body) is False


def test_find_misnamed_flags_a_parameter_named_for_the_thing_not_its_id() -> None:
    tree = ast.parse("def run(project: ID) -> None: ...\n")
    assert find_misnamed_id_annotations(tree) == [(1, "parameter", "project")]


def test_find_misnamed_flags_a_model_field() -> None:
    tree = ast.parse("class Run:\n    project: ID\n")
    assert find_misnamed_id_annotations(tree) == [(2, "field", "project")]


def test_find_misnamed_flags_a_return() -> None:
    tree = ast.parse("def mint_a_run() -> ID: ...\n")
    assert find_misnamed_id_annotations(tree) == [(1, "return of", "mint_a_run")]


def test_find_misnamed_flags_a_keyword_only_parameter() -> None:
    tree = ast.parse("def run(*, project: ID) -> None: ...\n")
    assert find_misnamed_id_annotations(tree) == [(1, "parameter", "project")]


def test_find_misnamed_passes_a_signature_that_names_every_id() -> None:
    tree = ast.parse("def find_run_id(project_id: ID, stage_ids: list[ID]) -> ID: ...\n")
    assert find_misnamed_id_annotations(tree) == []


def test_find_misnamed_ignores_a_plain_str_named_for_the_thing() -> None:
    tree = ast.parse("def run(project: str) -> None: ...\n")
    assert find_misnamed_id_annotations(tree) == []


def test_find_id_suffix_offenders_reports_the_path(tmp_path: Path) -> None:
    target = tmp_path / "other.py"
    target.write_text("def run(project: ID) -> None: ...\n", encoding="utf-8")
    assert find_id_suffix_offenders([target], tmp_path) == ["other.py:1  parameter project"]
