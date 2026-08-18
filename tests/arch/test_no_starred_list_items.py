"""Architecture: starred items in list displays only decrease.

Use list concatenation or an explicit loop so the operation is visible.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import scan_all_text

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Existing uses are recorded by file and count. Entries may only fall or disappear.
_ALLOWLIST: Mapping[str, int] = {
    "app/_arch_tests/test_a_project_is_referenced_by_its_id.py": 3,
    "app/_arch_tests/test_the_repo_root_is_owned.py": 3,
    "app/agents/tutorial/config.py": 1,
    "app/core/agent/agent.py": 2,
    "app/core/agent/store.py": 3,
    "app/evals/compatibility.py": 2,
    "app/models/schema.py": 2,
    "app/models/stages/signature.py": 6,
    "app/models/terms.py": 4,
    "app/runtime/_arch_tests/test_takes_objects_not_dirs.py": 2,
    "app/runtime/run_log.py": 2,
    "app/runtime/stages/join.py": 2,
    "app/runtime/stages/llm_transform.py": 1,
    "app/services/versioning.py": 5,
    "app/tools/prompt_fragments.py": 1,
    "app/web/_arch_tests/test_every_page_renders_a_trail.py": 1,
    "app/web/admin/spend.py": 2,
    "app/web/breadcrumbs.py": 9,
    "app/web/cmdk_palette.py": 7,
    "app/web/review_packet/lineage.py": 1,
    "app/web/review_packet/packet.py": 3,
    "scripts/dump_prompts.py": 1,
    "scripts/import_graph_report.py": 2,
    "scripts/lexicon.py": 1,
    "scripts/vocabulary.py": 2,
    "tests/arch/test_markdown_renderer_is_sealed.py": 2,
    "tests/arch/test_pandas_seam_ratchet.py": 3,
    "tests/arch/test_repeated_string_literals.py": 1,
    "tests/core/test_predicate.py": 1,
    "tests/models/stages/test_aggregate_columns.py": 1,
    "tests/models/stages/test_aggregate_signature.py": 1,
    "tests/models/stages/test_signature.py": 1,
    "tests/models/stages/test_union_columns.py": 1,
    "tests/runtime/test_aggregate_whole_frame.py": 6,
    "tests/runtime/test_hrq_cache.py": 2,
    "tests/runtime/test_hrq_declared_sort.py": 1,
    "tests/services/test_run_guide.py": 3,
    "tests/test_aggregate_lineage.py": 1,
    "tests/test_arch_failure_ledger.py": 1,
    "tests/test_handler_execution.py": 1,
    "tests/test_import_graph_report.py": 1,
    "tests/test_key_coverage.py": 2,
    "tests/test_migration_0006.py": 4,
    "tests/test_packet_lineage.py": 1,
    "tests/test_project_tools.py": 1,
    "tests/test_publish_trace_urls.py": 1,
    "tests/test_queue_view.py": 1,
    "tests/test_row_slicing.py": 1,
    "tests/test_run_cache_e2e.py": 3,
    "tests/test_seed_tutorial.py": 5,
    "tests/test_stage.py": 1,
    "tests/test_trace_contributor_groups.py": 1,
    "tests/test_tutorial_prompt.py": 4,
    "tests/test_workflow_test_is_a_real_run.py": 1,
}


def count_starred_list_items(paths: list[Path], repo_root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in paths:
        count = sum(
            isinstance(item, ast.Starred)
            for node in ast.walk(parse_module(path))
            if isinstance(node, ast.List)
            for item in node.elts
        )
        if count:
            counts[path.relative_to(repo_root).as_posix()] = count
    return counts


def find_ratchet_violations(
    counts: Mapping[str, int], allowlist: Mapping[str, int]
) -> list[str]:
    offenders = [
        f"{path}: {count} starred list item(s), not in the allowlist"
        for path, count in sorted(counts.items())
        if path not in allowlist
    ]
    offenders += [
        f"{path}: {counts[path]} starred list item(s), above its allowlist entry of {listed}"
        for path, listed in sorted(allowlist.items())
        if counts.get(path, 0) > listed
    ]
    offenders += [
        f"{path}: down to {counts.get(path, 0)} from {listed}; lower or remove the entry"
        for path, listed in sorted(allowlist.items())
        if counts.get(path, 0) < listed
    ]
    return offenders


def test_starred_list_items_only_decrease() -> None:
    counts = count_starred_list_items(scan_all_text((".py",)), _REPO_ROOT)
    offenders = find_ratchet_violations(counts, _ALLOWLIST)
    assert not offenders, (
        "starred list items obscure list construction. Use concatenation or an explicit loop; "
        "the allowlist may only shrink:\n  " + "\n  ".join(offenders)
    )


def test_count_starred_list_items_counts_only_direct_list_items(tmp_path: Path) -> None:
    target = tmp_path / "example.py"
    target.write_text("items = [*first, second]\ncall(*args)\n", encoding="utf-8")
    assert count_starred_list_items([target], tmp_path) == {"example.py": 1}


def test_find_ratchet_violations_flags_a_new_file() -> None:
    offenders = find_ratchet_violations({"new.py": 1}, {})
    assert offenders == ["new.py: 1 starred list item(s), not in the allowlist"]


def test_find_ratchet_violations_requires_a_lowered_entry() -> None:
    offenders = find_ratchet_violations({"old.py": 1}, {"old.py": 2})
    assert offenders == ["old.py: down to 1 from 2; lower or remove the entry"]
