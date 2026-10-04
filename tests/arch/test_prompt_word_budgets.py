"""Architecture: each ``app/**/*prompt*.py`` module holds a word budget over its str constants."""
from __future__ import annotations

import ast
from fnmatch import fnmatch
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import find_source_files_under
from arch.word_budgets import HOW_TO_PAY_FOR_GROWTH, find_budget_violations

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_PROMPT_MODULE_PATTERN = "*prompt*.py"
# llm.py holds SYSTEM_PROMPT, sent with every llm_transform call.
_PROMPT_MODULES_OUTSIDE_THE_PATTERN = ("app/runtime/llm.py",)

# Docstrings and the literal text of f-strings count: the measure is every str constant.
_WORD_BUDGETS: dict[str, int] = {
    "app/agents/compiler/prompt.py": 65,
    "app/agents/tutorial/prompt.py": 809,
    "app/compiler/review_guide_prompt.py": 677,
    "app/compiler/stage_tests_prompt.py": 1363,
    "app/core/prompt_template.py": 37,
    "app/models/tool_schema_prompts.py": 535,
    "app/reviewer/dedupe_prompt.py": 483,
    "app/reviewer/reviewers_prompt.py": 3523,
    "app/runtime/llm.py": 191,
    "app/tools/prompt_fragments.py": 1716,
}


def test_each_prompt_module_stays_within_its_word_budget() -> None:
    counts = {
        path.relative_to(_REPO_ROOT).as_posix(): count_string_constant_words(parse_module(path))
        for path in [*find_prompt_modules(_APP_ROOT),
                     *(_REPO_ROOT / module for module in _PROMPT_MODULES_OUTSIDE_THE_PATTERN)]
    }
    offenders = find_budget_violations(counts, _WORD_BUDGETS)
    assert not offenders, (
        "prompt word budgets, _WORD_BUDGETS in tests/arch/test_prompt_word_budgets.py:"
        "\n  " + "\n  ".join(offenders) + "\n" + HOW_TO_PAY_FOR_GROWTH
    )


def find_prompt_modules(root: Path) -> list[Path]:
    return [
        path for path in find_source_files_under(root)
        if fnmatch(path.name, _PROMPT_MODULE_PATTERN)
    ]


def count_string_constant_words(tree: ast.Module) -> int:
    return sum(
        len(node.value.split()) for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


# --- unit tests for the finder and the measure (red + green) -----------------


def test_find_prompt_modules_matches_prompt_anywhere_in_the_name(tmp_path: Path) -> None:
    names = ["a/prompt.py", "b/review_prompt.py", "c/prompt_fragments.py",
             "d/tool_schema_prompts.py", "e/runner.py", "f/prompt.md"]
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True)
        (tmp_path / name).write_text("", encoding="utf-8")
    found = [path.relative_to(tmp_path).as_posix() for path in find_prompt_modules(tmp_path)]
    assert found == [
        "a/prompt.py", "b/review_prompt.py", "c/prompt_fragments.py", "d/tool_schema_prompts.py",
    ]


def test_count_string_constant_words_counts_every_str_constant() -> None:
    source = '''"""Two words."""
GREETING = "three plain words"
LINE = f"one {name} two"
'''
    assert count_string_constant_words(ast.parse(source)) == 7


def test_count_string_constant_words_skips_comments_names_and_numbers() -> None:
    source = "# a comment of six words here\nword_count = 12\nprompt = b'bytes are not text'\n"
    assert count_string_constant_words(ast.parse(source)) == 0
