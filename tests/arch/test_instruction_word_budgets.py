"""Architecture: each AGENTS.md at the root or under ``app/`` holds a whitespace-word budget."""
from __future__ import annotations

from pathlib import Path

from arch.scope import scan_all_text
from arch.word_budgets import HOW_TO_PAY_FOR_GROWTH, find_budget_violations

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTRUCTION_FILE_NAME = "AGENTS.md"

_WORD_BUDGETS: dict[str, int] = {
    "AGENTS.md": 1150,
    "app/AGENTS.md": 2878,
    "app/runtime/AGENTS.md": 1952,
    "app/templates/AGENTS.md": 206,
}


def test_each_agents_md_stays_within_its_word_budget() -> None:
    counts = {
        path: count_whitespace_words(_REPO_ROOT / path)
        for path in find_instruction_files(scan_all_text((".md",)), _REPO_ROOT)
    }
    offenders = find_budget_violations(counts, _WORD_BUDGETS)
    assert not offenders, (
        "instruction word budgets, _WORD_BUDGETS in tests/arch/test_instruction_word_budgets.py:"
        "\n  " + "\n  ".join(offenders) + "\n" + HOW_TO_PAY_FOR_GROWTH
    )


def find_instruction_files(paths: list[Path], repo_root: Path) -> list[str]:
    relatives = [path.relative_to(repo_root) for path in paths]
    return sorted(
        relative.as_posix() for relative in relatives
        if relative.name == _INSTRUCTION_FILE_NAME
        and (len(relative.parts) == 1 or relative.parts[0] == "app")
    )


def count_whitespace_words(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").split())


# --- unit tests for the finder and the shared checker (red + green) ----------


def test_find_instruction_files_takes_the_root_and_every_depth_under_app(tmp_path: Path) -> None:
    names = ["AGENTS.md", "app/AGENTS.md", "app/web/deep/AGENTS.md", "critic/AGENTS.md",
             "app/CLAUDE.md", "docs/AGENTS.md.bak"]
    paths = [tmp_path / name for name in names]
    assert find_instruction_files(paths, tmp_path) == [
        "AGENTS.md", "app/AGENTS.md", "app/web/deep/AGENTS.md",
    ]


def test_count_whitespace_words_splits_on_any_whitespace(tmp_path: Path) -> None:
    file = tmp_path / "AGENTS.md"
    file.write_text("# Title\n\n- one `two`\tthree —  four\n", encoding="utf-8")
    assert count_whitespace_words(file) == 8


def test_find_budget_violations_passes_a_count_at_or_just_under_its_budget() -> None:
    assert find_budget_violations({"a.md": 100, "b.md": 96}, {"a.md": 100, "b.md": 100}) == []


def test_find_budget_violations_flags_a_count_over_its_budget() -> None:
    [offender] = find_budget_violations({"a.md": 101}, {"a.md": 100})
    assert offender == "a.md: 101 words, over its budget of 100"


def test_find_budget_violations_flags_a_budget_more_than_five_percent_above_the_count() -> None:
    assert find_budget_violations({"a.md": 100}, {"a.md": 105}) == []
    [offender] = find_budget_violations({"a.md": 100}, {"a.md": 106})
    assert offender.startswith("a.md: 100 words, more than 5% under its budget of 106")
    assert "lower the budget to 100" in offender


def test_find_budget_violations_flags_a_file_with_no_budget() -> None:
    [offender] = find_budget_violations({"app/new/AGENTS.md": 40}, {})
    assert offender.startswith("app/new/AGENTS.md: 40 words and no budget")


def test_find_budget_violations_flags_a_budget_whose_file_is_gone() -> None:
    [offender] = find_budget_violations({}, {"app/gone/AGENTS.md": 40})
    assert offender.startswith("app/gone/AGENTS.md: has a budget but no file")
