"""Architecture: derivation over a model's own collection belongs to the
model that owns it.

A table of ``ProtectedAttributeRule`` rows names an attribute (e.g. `stages`
on `Workflow`, `columns` on `TableSchema`) and the package that owns it. Code
outside that package may still READ the attribute (pass it along, measure its
length, iterate it plainly), but two things are never its job:

- Tier 1 (mutation): assigning to the attribute or a subscript of it, deleting
  it, or calling a mutating method on it (`.append`/`.remove`/`.clear`/
  `.pop`/`.insert`/`.extend`/`.sort`). This is a hard fail with no allowlist —
  reaching into another model's collection to change it in place is never a
  legitimate outside job, so there is nothing to grandfather.
- Tier 2 (derivation): looping or comprehending over the attribute to build
  an id-index, a projection of one field, or a filtered subset — the exact
  shape of a lookup/search primitive the owning model should expose instead.
  This is a ratchet allowlist: a pre-existing site is named explicitly, and a
  new one must be fixed (by adding or reusing an owner method), not added to
  the list.

Three distinct escape hatches appear below, and none substitutes for another:
the owner-package exemption (`rule.owner` — code inside the model's own
package is the implementation, not an outside caller), the Tier-2 ratchet
allowlist (`rule.allowlist` — a named pre-existing derivation site that may
only shrink, never grow), and `exempt_paths` (a file-scope exclusion for a
DIFFERENT model that happens to declare a same-named attribute — e.g.
`Draft.stages` in `app/services/drafts.py` — out of scope for that row on
both tiers because the file owns its OWN same-named attribute, not because
its re-derivation sites were grandfathered in).

Real review finding this codifies: `Workflow` already had nowhere to look up
one stage by id, so three call sites hand-rolled `{stage.id: stage for stage
in workflow.stages}` themselves (`app/runtime/runner.py`,
`app/evals/run_settings.py`, `app/evals/runner.py`) — duplicating a primitive
`Workflow` should own. The same shape recurs for `TableSchema.columns`:
service code re-derives "the column names of this schema" via
`[c.name for c in x.columns]` instead of a schema method, the same kind of
set-math re-derivation `TableSchema.subtract`/`is_subset_of` exist to avoid
for whole-schema comparisons.

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

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_EXEMPT_DIR_NAMES = {"tests", "_arch_tests", "__pycache__"}

_MUTATING_METHODS = frozenset({"append", "remove", "clear", "pop", "insert", "extend", "sort"})
_COMPREHENSION_TYPES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


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
    `Draft` entry below) — out of scope for this row entirely, on both
    tiers, the same way the owner package itself is.

    `allowlist` is the Tier-2 ratchet: pre-existing derivation sites named as
    `"<repo-relative-path>:<lineno>"`. Tier 1 (mutation) has no allowlist
    field — see the module docstring for why.
    """

    attribute: str
    owner: Path
    rationale: str
    receiver_is_relevant: Callable[[str | None], bool] = _accept_any_receiver
    exempt_paths: frozenset[Path] = field(default_factory=frozenset)
    allowlist: frozenset[str] = field(default_factory=frozenset)


def _ends_with_schema(receiver: str | None) -> bool:
    return receiver is not None and receiver.endswith("schema")


_RULES: tuple[ProtectedAttributeRule, ...] = (
    ProtectedAttributeRule(
        attribute="columns",
        owner=_REPO_ROOT / "app" / "core" / "models",
        rationale=(
            "TableSchema.columns is app/core/models' to search, project, or "
            "filter — a caller that needs 'the column names of this schema' "
            "or 'the columns matching X' is re-deriving a primitive the "
            "schema should expose (see TableSchema.subtract/is_subset_of for "
            "the whole-schema equivalent already avoided this way)."
        ),
        receiver_is_relevant=_ends_with_schema,
        # Pre-existing sites that project column names straight off a
        # schema's .columns instead of a schema method. A ratchet: a new
        # offender must call (or add) a TableSchema method, not extend this.
        allowlist=frozenset(
            {
                "app/runtime/stages/execution.py:231",
                "app/runtime/stages/human_review_queue.py:198",
                "app/runtime/stage_tests.py:199",
                "app/runtime/stage_tests.py:234",
                "app/web/routers/evals.py:138",
            }
        ),
    ),
    ProtectedAttributeRule(
        attribute="stages",
        owner=_REPO_ROOT / "app" / "core" / "models",
        rationale=(
            "Workflow.stages is app/core/models' to index, project, or "
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
        # (`d.stages = kept + [stage]`) would hard-fail Tier 1, which has no
        # allowlist to absorb it. Draft's stages never belonged in this row.
        exempt_paths=frozenset({_REPO_ROOT / "app" / "services" / "drafts.py"}),
        # Pre-existing site that builds an id-keyed index over workflow.stages
        # outside the three converted call sites. A ratchet: a new offender
        # must call Workflow.index_stages_by_id, not extend this.
        allowlist=frozenset(
            {
                "app/services/stage_edit.py:56",
            }
        ),
    ),
)


def find_source_files(root: Path, rule: ProtectedAttributeRule) -> list[Path]:
    """The .py files under `root` this rule governs: every file except those
    under its owner package or one of its exempt paths, and except tests/,
    _arch_tests/, and __pycache__ anywhere in the tree."""
    skip = (rule.owner, *rule.exempt_paths)
    files = [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(part in _EXEMPT_DIR_NAMES for part in path.relative_to(root).parts)
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
    """(lineno, description) for every Tier-1 mutation of `attribute` in
    `tree`: an assignment/augmented-assignment/del targeting it (or a
    subscript of it), or a call to one of the list-mutating methods on it."""
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


def find_derivation_sites(
    tree: ast.Module, attribute: str, receiver_is_relevant: Callable[[str | None], bool] = _accept_any_receiver,
) -> list[tuple[int, str]]:
    """(lineno, kind) for every Tier-2 derivation over `attribute` in `tree`:
    a for-loop/comprehension/generator that indexes, projects, or filters by
    an element's own attribute, or a filter()/sorted() call that pairs
    `attribute` with a predicate/key. Plain pass-through iteration, len(), and
    a bare list()/set() conversion are not derivation — nothing to flag."""
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, _COMPREHENSION_TYPES):
            kind = _classify_comprehension_derivation(node, attribute, receiver_is_relevant)
            if kind is not None:
                offenders.append((node.lineno, kind))
        elif isinstance(node, ast.For):
            kind = _classify_for_loop_derivation(node, attribute, receiver_is_relevant)
            if kind is not None:
                offenders.append((node.lineno, kind))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            kind = _classify_predicate_call_derivation(node, attribute, receiver_is_relevant)
            if kind is not None:
                offenders.append((node.lineno, kind))
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


# --- Tier-2 comprehension/generator/for-loop shapes -------------------------


def _collect_loop_var_names(target: ast.expr) -> frozenset[str]:
    """Every name a `for` target binds: a bare name, or every name inside a
    tuple-unpacking target (`for a, b in ...`)."""
    if isinstance(target, ast.Name):
        return frozenset({target.id})
    if isinstance(target, ast.Tuple):
        return frozenset().union(*(_collect_loop_var_names(elt) for elt in target.elts)) if target.elts else frozenset()
    return frozenset()


def _is_attr_on_var(node: ast.AST, var_names: frozenset[str]) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in var_names


def _contains_attr_on_var(node: ast.AST, var_names: frozenset[str]) -> bool:
    return any(_is_attr_on_var(n, var_names) for n in ast.walk(node))


def _is_boolean_test_on_var(node: ast.expr, var_names: frozenset[str]) -> bool:
    """True when `node` is a boolean or comparison expression (`s.id ==
    target`, `s.active and s.ready`) that reads one of `var_names`'s own
    attributes — the shape `any()`/`all()` wrap around a GeneratorExp's elt
    as their sole predicate. This is the same "test the loop variable" shape
    a comprehension's `if` clause expresses via `gen.ifs`; here it is carried
    in the elt/body instead, with no `if` clause to hold it."""
    return isinstance(node, (ast.Compare, ast.BoolOp)) and _contains_attr_on_var(node, var_names)


def _find_matching_generator(
    node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    attribute: str,
    receiver_is_relevant: Callable[[str | None], bool],
) -> ast.comprehension | None:
    """The `for` clause of `node` whose iterable IS the protected attribute,
    if any (a comprehension may have several `for` clauses; only one need
    match for the comprehension to be in scope for this rule)."""
    for gen in node.generators:
        if _is_protected_access(gen.iter, attribute, receiver_is_relevant):
            return gen
    return None


def _classify_comprehension_derivation(
    node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    attribute: str,
    receiver_is_relevant: Callable[[str | None], bool],
) -> str | None:
    gen = _find_matching_generator(node, attribute, receiver_is_relevant)
    if gen is None:
        return None
    var_names = _collect_loop_var_names(gen.target)
    if any(_contains_attr_on_var(cond, var_names) for cond in gen.ifs):
        return "filtering"
    if isinstance(node, ast.DictComp) and _is_attr_on_var(node.key, var_names):
        return "indexing"
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) and _is_attr_on_var(node.elt, var_names):
        return "projection"
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) and _is_boolean_test_on_var(
        node.elt, var_names
    ):
        return "filtering"
    return None


def _classify_for_loop_derivation(
    node: ast.For, attribute: str, receiver_is_relevant: Callable[[str | None], bool],
) -> str | None:
    """The statement-loop equivalent of `_classify_comprehension_derivation`: a
    plain `for x in y:` that reaches the same shapes by hand (an `if` on the
    loop variable's attribute, a dict keyed by one, or a projecting
    `.append`) rather than through a comprehension."""
    if not _is_protected_access(node.iter, attribute, receiver_is_relevant):
        return None
    var_names = _collect_loop_var_names(node.target)
    for stmt in node.body:
        if isinstance(stmt, ast.If) and _contains_attr_on_var(stmt.test, var_names):
            return "filtering"
        if _is_index_assignment(stmt, var_names):
            return "indexing"
        if _is_projecting_append(stmt, var_names):
            return "projection"
    return None


def _is_index_assignment(stmt: ast.stmt, var_names: frozenset[str]) -> bool:
    return isinstance(stmt, ast.Assign) and any(
        isinstance(t, ast.Subscript) and _is_attr_on_var(t.slice, var_names) for t in stmt.targets
    )


def _is_projecting_append(stmt: ast.stmt, var_names: frozenset[str]) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    call = stmt.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "append"
        and any(_is_attr_on_var(arg, var_names) for arg in call.args)
    )


# --- Tier-2 filter()/sorted() shape (no comprehension involved) -------------


def _classify_predicate_call_derivation(
    node: ast.Call, attribute: str, receiver_is_relevant: Callable[[str | None], bool],
) -> str | None:
    """filter(predicate, protected_attr) or sorted(protected_attr, key=...):
    the attribute passed straight in, paired with an explicit predicate/key.

    any()/all()/next() wrapping a generator expression over the attribute
    (`any(s.id == target for s in workflow.stages)`) need no special-casing
    here: that generator is its own GeneratorExp node, walked and classified
    by `_classify_comprehension_derivation`, which treats a boolean/
    comparison expression in the elt as filtering — the same test any()/all()
    perform — not just a bare-attribute elt as projection."""
    assert isinstance(node.func, ast.Name)
    name = node.func.id
    if name == "filter" and len(node.args) >= 2:
        if _is_protected_access(node.args[1], attribute, receiver_is_relevant):
            return "predicate-call"
        return None
    if name == "sorted" and node.args and any(kw.arg == "key" for kw in node.keywords):
        if _is_protected_access(node.args[0], attribute, receiver_is_relevant):
            return "predicate-call"
        return None
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
        "allowlist for this tier:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("rule", _RULES, ids=describe_rule_id)
def test_protected_attribute_is_not_re_derived_from_outside_its_owner(rule: ProtectedAttributeRule) -> None:
    offenders = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}  {description}"
        for path in find_source_files(_APP_ROOT, rule)
        for lineno, description in find_derivation_sites(
            parse_module(path), rule.attribute, rule.receiver_is_relevant
        )
        if f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}" not in rule.allowlist
    ]
    assert not offenders, (
        f"{rule.rationale}\n"
        f"searching/selecting/indexing over .{rule.attribute} re-derives a primitive "
        "the owner should expose:\n  " + "\n  ".join(offenders)
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


def test_find_derivation_sites_flags_index_build() -> None:
    tree = ast.parse("by_id = {s.id: s for s in workflow.stages}\n")
    assert find_derivation_sites(tree, "stages") == [(1, "indexing")]


def test_find_derivation_sites_flags_projection() -> None:
    tree = ast.parse("ids = [s.id for s in workflow.stages]\n")
    assert find_derivation_sites(tree, "stages") == [(1, "projection")]


def test_find_derivation_sites_flags_filtering() -> None:
    tree = ast.parse("matches = [s for s in workflow.stages if s.id == target]\n")
    assert find_derivation_sites(tree, "stages") == [(1, "filtering")]


def test_find_derivation_sites_flags_any_call_with_comparison_predicate() -> None:
    """`any()`'s sole argument is a GeneratorExp whose elt is a comparison,
    not a comprehension `if` clause — the gap this rule used to miss."""
    tree = ast.parse("found = any(s.id == t for s in x.stages)\n")
    assert find_derivation_sites(tree, "stages") == [(1, "filtering")]


def test_find_derivation_sites_allows_any_call_with_no_attribute_use() -> None:
    """The generator elt references no attribute of the loop variable at
    all, so there is nothing to derive from `.stages` — not flagged."""
    tree = ast.parse("found = any(True for _ in x.stages)\n")
    assert find_derivation_sites(tree, "stages") == []


def test_find_derivation_sites_allows_a_pass_through_render_loop() -> None:
    tree = ast.parse("for stage in workflow.stages:\n    render(stage)\n")
    assert find_derivation_sites(tree, "stages") == []


def test_find_derivation_sites_allows_len() -> None:
    tree = ast.parse("n = len(workflow.stages)\n")
    assert find_derivation_sites(tree, "stages") == []


def test_find_derivation_sites_allows_a_dataframe_columns_receiver() -> None:
    tree = ast.parse("names = [c for c in df.columns]\n")
    offenders = find_derivation_sites(tree, "columns", _ends_with_schema)
    assert offenders == []


def test_find_derivation_sites_flags_a_schema_columns_receiver_when_deriving() -> None:
    tree = ast.parse("names = [c.name for c in schema.columns]\n")
    offenders = find_derivation_sites(tree, "columns", _ends_with_schema)
    assert offenders == [(1, "projection")]


def test_find_derivation_sites_flags_filter_call_with_predicate() -> None:
    tree = ast.parse("kept = filter(is_active, workflow.stages)\n")
    assert find_derivation_sites(tree, "stages") == [(1, "predicate-call")]


def test_find_derivation_sites_flags_sorted_call_with_key() -> None:
    tree = ast.parse("ordered = sorted(workflow.stages, key=lambda s: s.id)\n")
    assert find_derivation_sites(tree, "stages") == [(1, "predicate-call")]


def test_find_derivation_sites_allows_sorted_call_without_key() -> None:
    tree = ast.parse("ordered = sorted(workflow.stages)\n")
    assert find_derivation_sites(tree, "stages") == []


def test_find_derivation_sites_allows_a_bare_name_receiver() -> None:
    """`stages` here is a plain local variable, not an attribute access — the
    rule is about reaching into a MODEL's collection, which a bare name never
    does regardless of what it happens to be called."""
    tree = ast.parse("by_id = {s.id: s for s in stages}\n")
    assert find_derivation_sites(tree, "stages") == []


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
