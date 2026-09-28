"""Architecture: each file under ``app/`` is held to its count of ``dict[str, Any]``."""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

# Occurrences per file when this rule landed. A ratchet: lower or remove an entry, never raise one.
_GRANDFATHERED_COUNTS: dict[str, int] = {
    "app/cli.py": 2,
    "app/compiler/stage_tests_submission.py": 1,
    "app/core/agent/agent.py": 6,
    "app/core/agent/bound_tool.py": 7,
    "app/core/agent/diagnostics.py": 11,
    "app/core/agent/registry.py": 5,
    "app/core/agent/sdk_engine.py": 6,
    "app/core/agent/session.py": 2,
    "app/core/agent/store.py": 6,
    "app/core/agent/turns.py": 1,
    "app/core/frames.py": 5,
    "app/core/json_types.py": 1,
    "app/core/row_search.py": 2,
    "app/core/source_files.py": 1,
    "app/evals/runner.py": 1,
    "app/evals/scoring.py": 3,
    "app/mcp/server.py": 12,
    "app/models/named_schemas.py": 3,
    "app/models/records/draft.py": 1,
    "app/models/records/eval_run.py": 1,
    "app/models/records/run_manifest.py": 3,
    "app/models/records/workflow_version.py": 2,
    "app/models/run_manifest.py": 6,
    "app/models/schema.py": 3,
    "app/models/stage.py": 2,
    "app/models/stages/llm_transform.py": 1,
    "app/models/stages/stage_base.py": 3,
    "app/models/stages/stage_tests.py": 2,
    "app/models/workflow.py": 3,
    "app/runtime/code.py": 1,
    "app/runtime/llm.py": 5,
    "app/runtime/manifest.py": 1,
    "app/runtime/run_log.py": 3,
    "app/runtime/runner.py": 7,
    "app/runtime/spans.py": 1,
    "app/runtime/stage_tests.py": 6,
    "app/runtime/stages/__init__.py": 1,
    "app/runtime/stages/aggregate.py": 1,
    "app/runtime/stages/execution.py": 1,
    "app/runtime/stages/filter_rows.py": 1,
    "app/runtime/stages/input_data.py": 1,
    "app/runtime/stages/llm_transform.py": 4,
    "app/runtime/stages/starlark_marshal.py": 2,
    "app/runtime/starlark_code.py": 1,
    "app/runtime/trace.py": 13,
    "app/runtime/validation.py": 1,
    "app/services/project.py": 2,
    "app/services/review_packet/views.py": 10,
    "app/services/run.py": 8,
    "app/services/run_guide.py": 2,
    "app/services/versioning.py": 1,
    "app/services/workflow_test.py": 1,
    "app/services/workspace.py": 2,
    "app/tools/editing.py": 1,
    "app/tools/shared.py": 9,
    "app/tools/submitted_stage.py": 2,
    "app/web/column_order.py": 2,
    "app/web/diagrams.py": 13,
    "app/web/judgment_view.py": 1,
    "app/web/lineage_coordinate.py": 2,
    "app/web/loading.py": 14,
    "app/web/project_cards.py": 1,
    "app/web/review_packet/lineage.py": 3,
    "app/web/review_packet/packet.py": 3,
    "app/web/review_packet/pages.py": 3,
    "app/web/routers/evals.py": 3,
    "app/web/routers/run_lineage.py": 3,
    "app/web/routers/run_stage.py": 2,
    "app/web/routers/runs.py": 1,
    "app/web/run_events.py": 8,
    "app/web/run_stage_panel.py": 1,
    "app/web/run_stage_view.py": 9,
    "app/web/stage_test_views.py": 9,
    "app/web/trace_row_diff.py": 4,
    "app/web/trace_view.py": 17,
}

# Module path -> the written reason it may spell dict[str, Any]. Only the owner adds one.
_BOUNDARY_MODULES: dict[str, str] = {}


def find_dict_str_any_lines(tree: ast.Module) -> list[int]:
    subscripts = [node for node in ast.walk(tree) if isinstance(node, ast.Subscript)]
    return sorted(node.lineno for node in subscripts if _is_dict_str_any(node))


def find_lines_by_path(paths: list[Path], repo_root: Path) -> dict[str, list[int]]:
    lines_by_path = {
        path.relative_to(repo_root).as_posix(): find_dict_str_any_lines(parse_module(path))
        for path in paths
    }
    return {path: lines for path, lines in lines_by_path.items() if lines}


def find_ratchet_violations(
    lines_by_path: dict[str, list[int]],
    grandfathered: dict[str, int],
    boundaries: dict[str, str],
) -> list[str]:
    counted_lines_by_path = {
        path: lines for path, lines in lines_by_path.items() if path not in boundaries
    }
    offenders = [
        _describe_new_occurrence(path, lines, grandfathered.get(path, 0))
        for path, lines in sorted(counted_lines_by_path.items())
        if len(lines) > grandfathered.get(path, 0)
    ]
    offenders += [
        _describe_stale_count(path, len(counted_lines_by_path.get(path, [])), allowed)
        for path, allowed in sorted(grandfathered.items())
        if len(counted_lines_by_path.get(path, [])) < allowed
    ]
    offenders += [
        f"{path}  (in _BOUNDARY_MODULES but spells no dict[str, Any]) — remove the entry"
        for path in sorted(boundaries)
        if path not in lines_by_path
    ]
    return offenders


def test_no_file_under_app_adds_a_dict_str_any() -> None:
    lines_by_path = find_lines_by_path(find_source_files_under(_APP_ROOT), _REPO_ROOT)
    offenders = find_ratchet_violations(lines_by_path, _GRANDFATHERED_COUNTS, _BOUNDARY_MODULES)
    assert not offenders, (
        "dict[str, Any] ratchet: a dict with a known set of keys is a missing model. Three ways "
        "out: (1) define a Pydantic model for it; (2) reuse an existing model; (3) for a "
        "genuinely dynamic JSON bundle, reuse an alias — JsonDict from "
        "app/core/json_types.py, or an existing TypeUnsafe* alias. A new alias needs a "
        "_BOUNDARY_MODULES entry with a written reason, which is the owner's decision. "
        "_GRANDFATHERED_COUNTS in tests/arch/test_no_dict_str_any.py may only fall — never raise "
        "an entry or add a file:\n  " + "\n  ".join(offenders)
    )


# --- detection ---------------------------------------------------------------


def _is_dict_str_any(node: ast.Subscript) -> bool:
    if not _is_one_of(node.value, {"dict", "Dict"}):
        return False
    key_and_value = node.slice
    return (
        isinstance(key_and_value, ast.Tuple)
        and len(key_and_value.elts) == 2
        and _is_one_of(key_and_value.elts[0], {"str"})
        and _is_one_of(key_and_value.elts[1], {"Any"})
    )


def _is_one_of(node: ast.expr, names: set[str]) -> bool:
    # `typing.Dict` and `t.Any` parse as an Attribute.
    if isinstance(node, ast.Attribute):
        return node.attr in names
    return isinstance(node, ast.Name) and node.id in names


# --- offender messages -------------------------------------------------------


def _describe_new_occurrence(path: str, lines: list[int], allowed: int) -> str:
    where = "\n      ".join(f"{path}:{line}" for line in lines)
    if allowed == 0:
        return f"{path}  (not in _GRANDFATHERED_COUNTS, so its first one fails):\n      {where}"
    return f"{path}  (holds {len(lines)}, over its grandfathered {allowed}):\n      {where}"


def _describe_stale_count(path: str, actual: int, allowed: int) -> str:
    if actual == 0:
        return f"{path}  (now 0, entry {allowed}) — remove the stale _GRANDFATHERED_COUNTS entry"
    return f"{path}  (now {actual}, entry {allowed}) — lower the _GRANDFATHERED_COUNTS entry to {actual}"


# --- unit tests for the finder and the ratchet (red + green) ------------------

_SNIPPET_WITH_EVERY_FORM = """\
from typing import Any, TypeAlias, cast
import typing

def take(payload: dict[str, Any]) -> None: ...
def give() -> dict[str, Any]: ...
local: dict[str, Any] = {}
class Holder:
    field: dict[str, Any]
type Statement = dict[str, Any]
Annotated: TypeAlias = dict[str, Any]
Implicit = dict[str, Any]
nested: list[dict[str, Any]] | None = None
legacy: typing.Dict[str, typing.Any] = {}
cast(dict[str, Any], {})
"""


def test_find_dict_str_any_lines_flags_every_form() -> None:
    assert find_dict_str_any_lines(ast.parse(_SNIPPET_WITH_EVERY_FORM)) == [4, 5, 6, 8, 9, 10, 11, 12, 13, 14]


def test_find_dict_str_any_lines_counts_two_on_one_line() -> None:
    tree = ast.parse("def f(a: dict[str, Any]) -> dict[str, Any]: ...\n")
    assert find_dict_str_any_lines(tree) == [1, 1]


def test_find_dict_str_any_lines_ignores_other_shapes_and_prose() -> None:
    tree = ast.parse(
        '"""Returns dict[str, Any]."""\n'
        "# dict[str, Any]\n"
        "a: dict[str, int] = {}\n"
        "b: Mapping[str, Any] = {}\n"
        "c: dict[int, Any] = {}\n"
        "d: JsonDict = {}\n"
    )
    assert find_dict_str_any_lines(tree) == []


def test_ratchet_flags_a_fixture_module_carrying_one_new_occurrence(tmp_path: Path) -> None:
    lines_by_path = _write_fixture_module(tmp_path, "a: dict[str, Any]\nb: dict[str, Any]\n")
    [offender] = find_ratchet_violations(lines_by_path, {"app/fixture.py": 1}, {})
    assert "over its grandfathered 1" in offender
    assert "app/fixture.py:1" in offender and "app/fixture.py:2" in offender


def test_ratchet_flags_the_first_occurrence_in_an_unlisted_file(tmp_path: Path) -> None:
    lines_by_path = _write_fixture_module(tmp_path, "x = 1\na: dict[str, Any]\n")
    [offender] = find_ratchet_violations(lines_by_path, {}, {})
    assert "not in _GRANDFATHERED_COUNTS" in offender and "app/fixture.py:2" in offender


def test_ratchet_passes_a_file_at_its_count() -> None:
    assert find_ratchet_violations({"app/a.py": [3, 9]}, {"app/a.py": 2}, {}) == []


def test_ratchet_flags_a_count_that_fell_as_stale() -> None:
    [offender] = find_ratchet_violations({"app/a.py": [3]}, {"app/a.py": 2}, {})
    assert "lower the _GRANDFATHERED_COUNTS entry to 1" in offender


def test_ratchet_flags_an_entry_for_a_file_that_holds_none() -> None:
    [offender] = find_ratchet_violations({}, {"app/gone.py": 2}, {})
    assert "remove the stale _GRANDFATHERED_COUNTS entry" in offender


def test_ratchet_skips_a_boundary_module() -> None:
    boundaries = {"app/edge.py": "parses foreign JSON"}
    assert find_ratchet_violations({"app/edge.py": [1, 2]}, {}, boundaries) == []


def test_ratchet_flags_a_boundary_module_that_spells_none() -> None:
    [offender] = find_ratchet_violations({}, {}, {"app/edge.py": "parses foreign JSON"})
    assert "in _BOUNDARY_MODULES" in offender and "remove the entry" in offender


def test_ratchet_flags_a_count_kept_for_a_boundary_module() -> None:
    boundaries = {"app/edge.py": "parses foreign JSON"}
    [offender] = find_ratchet_violations({"app/edge.py": [1]}, {"app/edge.py": 1}, boundaries)
    assert "remove the stale _GRANDFATHERED_COUNTS entry" in offender


def _write_fixture_module(tmp_path: Path, source: str) -> dict[str, list[int]]:
    module = tmp_path / "app" / "fixture.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")
    return find_lines_by_path([module], tmp_path)
