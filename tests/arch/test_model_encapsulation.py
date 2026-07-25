"""Architecture: a protected model attribute is never mutated from outside
its owning package.

A table of ``ProtectedAttributeRule`` rows names an attribute (e.g. `stages`
on `Workflow`, `columns` on `TableSchema`) and the package that owns it. Code
outside that package may still READ the attribute (pass it along, measure its
length, iterate it plainly), but assigning to it or a subscript of it,
deleting it, or calling a mutating method on it (`.append`/`.remove`/
`.clear`/`.pop`/`.insert`/`.extend`/`.sort`) is never its job — reaching into
another model's collection to change it in place is never a legitimate
outside job. This is a hard fail with no allowlist.

Two escape hatches appear below, and neither substitutes for the other: the
owner-package exemption (`rule.owner` — code inside the model's own package
is the implementation, not an outside caller), and `exempt_paths` (a
file-scope exclusion for a DIFFERENT model that happens to declare a
same-named attribute — e.g. `Draft.stages` in `app/services/drafts.py` — out
of scope for that row because the file owns its OWN same-named attribute).

Detection is name-based AST matching (types can't be resolved from a bare
`.attr` access), which invites two kinds of over-match, each handled
differently:

- `columns` collides with `pandas.DataFrame.columns`, used throughout the
  runtime on dataframes named `df`/`merged`/`out`/etc. A row may set
  `receiver_is_relevant` to filter by the identifier the attribute was read
  off (`schema.columns` -> "schema"); the `columns` row below only counts a
  receiver whose identifier IS or ENDS WITH "schema" (schema, table_schema,
  output_schema, input_schema, ...), which a `df`/`merged`/`out` receiver
  never does. `stages` has no realistic namesake to exclude, so its row uses
  no filter.
- `stages` collides with OTHER models that happen to declare their own
  `stages` field — see `_RULES` below for the one identified (and exempted)
  case, `Draft`.
"""
from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from arch._helpers import parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

_MUTATING_METHODS = frozenset({"append", "remove", "clear", "pop", "insert", "extend", "sort"})


def _accept_any_receiver(receiver: str | None) -> bool:
    return True


@dataclass(frozen=True)
class ProtectedAttributeRule:
    """One row: a model attribute that only its owning package may reach into.

    `receiver_is_relevant` filters which attribute accesses count: given the
    identifier of the object the attribute was read off (`schema.columns` ->
    "schema"; `df.columns` -> "df"; None when the receiver has no simple
    identifier, e.g. a chained call result), it decides whether this access
    is plausibly about the attribute this row protects rather than a
    same-named but unrelated one. The default accepts every receiver.

    `exempt_paths` names other files that declare their OWN attribute of the
    same name and are that attribute's sole owner (see the `stages` row's
    `Draft` entry below) — out of scope for this row entirely, the same way
    the owner package itself is.
    """

    attribute: str
    owner: Path
    rationale: str
    receiver_is_relevant: Callable[[str | None], bool] = _accept_any_receiver
    exempt_paths: frozenset[Path] = field(default_factory=frozenset)


def _ends_with_schema(receiver: str | None) -> bool:
    return receiver is not None and receiver.endswith("schema")


_RULES: tuple[ProtectedAttributeRule, ...] = (
    ProtectedAttributeRule(
        attribute="columns",
        owner=_REPO_ROOT / "app" / "core" / "models",
        rationale=(
            "TableSchema.columns is app/models' to search, project, or "
            "filter — a caller that needs 'the column names of this schema' "
            "or 'the columns matching X' is re-deriving a primitive the "
            "schema should expose (see TableSchema.subtract/is_subset_of for "
            "the whole-schema equivalent already avoided this way)."
        ),
        receiver_is_relevant=_ends_with_schema,
    ),
    ProtectedAttributeRule(
        attribute="stages",
        owner=_REPO_ROOT / "app" / "core" / "models",
        rationale=(
            "Workflow.stages is app/models' to index, project, or "
            "filter — a caller that needs 'the stage with this id' or 'the "
            "ids of these stages' is re-deriving a primitive Workflow should "
            "expose (see Workflow.index_stages_by_id, added for exactly the "
            "three sites that used to hand-roll {stage.id: stage for stage "
            "in workflow.stages})."
        ),
        # app/services/drafts.py's `Draft` is a SEPARATE Pydantic model (not
        # Workflow) with its own `stages: list[Stage]` field; the module is
        # documented as that field's sole lifecycle manager (mutable scratch
        # space an agent/edit-buffer assembles before freezing into a
        # version — see the module docstring). Name-based matching can't
        # tell `d.stages` (Draft) from `workflow.stages` (Workflow) apart, so
        # without this exemption Draft's own legitimate self-mutation
        # (`d.stages = kept + [stage]`) would hard-fail the mutation check,
        # which has no allowlist to absorb it. Draft's stages never belonged
        # in this row.
        exempt_paths=frozenset({
            _REPO_ROOT / "app" / "services" / "drafts.py",
        }),
    ),
)


def find_source_files(root: Path, rule: ProtectedAttributeRule) -> list[Path]:
    """The .py files under `root` this rule governs: the shared arch-test
    scope (see arch.scope for the base tests/_arch_tests/__pycache__
    exemptions), minus every file under this rule's own owner package or one
    of its exempt paths."""
    skip = (rule.owner, *rule.exempt_paths)
    files = [
        path
        for path in find_source_files_under(root)
        if not any(path == skipped or skipped in path.parents for skipped in skip)
    ]
    if not files:
        raise ValueError(
            f"protected-attribute rule {rule.attribute!r} governs no source files under "
            f"{root} — its owner/exempt_paths are excluding the whole tree"
        )
    return files


def find_mutation_sites(
    tree: ast.Module, attribute: str, receiver_is_relevant: Callable[[str | None], bool] = _accept_any_receiver,
) -> list[tuple[int, str]]:
    """(lineno, description) for every mutation of `attribute` in `tree`: an
    assignment/augmented-assignment/del targeting it (or a subscript of it),
    or a call to one of the list-mutating methods on it."""
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                site = _describe_mutated_protected_target(target, attribute, receiver_is_relevant)
                if site is not None:
                    offenders.append((node.lineno, f"assignment to {site}"))
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                site = _describe_mutated_protected_target(target, attribute, receiver_is_relevant)
                if site is not None:
                    offenders.append((node.lineno, f"del {site}"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATING_METHODS
            and _is_protected_access(node.func.value, attribute, receiver_is_relevant)
        ):
            offenders.append((node.lineno, f".{attribute}.{node.func.attr}()"))
    return offenders


# --- shared attribute-access matching ---------------------------------------


def _identify_receiver(value: ast.expr) -> str | None:
    """The identifier `value` (the object side of an attribute access) reads
    as: a bare name's own id, or a chained attribute's own (rightmost) attr.
    None for anything else (e.g. a call result) — nothing to filter on."""
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _is_protected_access(
    node: ast.expr, attribute: str, receiver_is_relevant: Callable[[str | None], bool],
) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attribute
        and receiver_is_relevant(_identify_receiver(node.value))
    )


def _describe_mutated_protected_target(
    target: ast.expr, attribute: str, receiver_is_relevant: Callable[[str | None], bool],
) -> str | None:
    """Describe `target` if it IS the protected attribute or a subscript of
    it — the two shapes an assignment/del target can take to mutate it."""
    if _is_protected_access(target, attribute, receiver_is_relevant):
        return f".{attribute}"
    if isinstance(target, ast.Subscript) and _is_protected_access(target.value, attribute, receiver_is_relevant):
        return f".{attribute}[...]"
    return None


# --- the rules, run against the real tree -----------------------------------


def describe_rule_id(rule: ProtectedAttributeRule) -> str:
    return rule.attribute


@pytest.mark.parametrize("rule", _RULES, ids=describe_rule_id)
def test_protected_attribute_is_never_mutated_from_outside_its_owner(rule: ProtectedAttributeRule) -> None:
    offenders = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}  {description}"
        for path in find_source_files(_APP_ROOT, rule)
        for lineno, description in find_mutation_sites(parse_module(path), rule.attribute, rule.receiver_is_relevant)
    ]
    assert not offenders, (
        f"{rule.rationale}\n"
        f"mutating .{rule.attribute} from outside its owner is never allowed, with no "
        "allowlist:\n  " + "\n  ".join(offenders)
    )


# --- unit tests for the checker, on inline snippets (red + green) ----------


def test_find_mutation_sites_flags_assignment() -> None:
    tree = ast.parse("workflow.stages = new_stages\n")
    assert find_mutation_sites(tree, "stages") == [(1, "assignment to .stages")]


def test_find_mutation_sites_flags_augmented_assignment() -> None:
    tree = ast.parse("workflow.stages += [stage]\n")
    assert find_mutation_sites(tree, "stages") == [(1, "assignment to .stages")]


def test_find_mutation_sites_flags_subscript_assignment() -> None:
    tree = ast.parse("workflow.stages[0] = stage\n")
    assert find_mutation_sites(tree, "stages") == [(1, "assignment to .stages[...]")]


def test_find_mutation_sites_flags_del() -> None:
    tree = ast.parse("del workflow.stages[0]\n")
    assert find_mutation_sites(tree, "stages") == [(1, "del .stages[...]")]


def test_find_mutation_sites_flags_mutating_method_call() -> None:
    tree = ast.parse("workflow.stages.append(stage)\n")
    assert find_mutation_sites(tree, "stages") == [(1, ".stages.append()")]


def test_find_mutation_sites_ignores_a_plain_read() -> None:
    tree = ast.parse("n = len(workflow.stages)\n")
    assert find_mutation_sites(tree, "stages") == []


def test_find_source_files_excludes_owner_and_exempt_paths(tmp_path: Path) -> None:
    owner_dir = tmp_path / "core" / "models"
    owner_dir.mkdir(parents=True)
    (owner_dir / "workflow.py").write_text("")
    exempt_file = tmp_path / "services" / "drafts.py"
    exempt_file.parent.mkdir(parents=True)
    exempt_file.write_text("")
    other_file = tmp_path / "services" / "runner.py"
    other_file.write_text("")
    rule = ProtectedAttributeRule(
        attribute="stages",
        owner=owner_dir,
        rationale="test",
        exempt_paths=frozenset({exempt_file}),
    )
    assert find_source_files(tmp_path, rule) == [other_file]


def test_find_source_files_raises_when_scope_is_empty(tmp_path: Path) -> None:
    owner_dir = tmp_path / "only_file_lives_here"
    owner_dir.mkdir(parents=True)
    (owner_dir / "m.py").write_text("")
    rule = ProtectedAttributeRule(attribute="stages", owner=owner_dir, rationale="test")
    with pytest.raises(ValueError, match="governs no source files"):
        find_source_files(tmp_path, rule)
