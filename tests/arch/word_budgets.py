"""Word budgets: a file's word count may not pass its budget, nor sit more than 5% under it."""
from __future__ import annotations

SLACK_PERCENT = 5

HOW_TO_PAY_FOR_GROWTH = (
    "Pay for growth in the same diff: cut words from the same file, or raise its entry in "
    "_WORD_BUDGETS so the reviewer sees the new number. Raising or adding a budget is a "
    "human decision (AGENTS.md: never weaken an arch test without human approval)."
)


def find_budget_violations(counts: dict[str, int], budgets: dict[str, int]) -> list[str]:
    budgeted = sorted(path for path in counts if path in budgets)
    offenders = [
        _describe_unbudgeted(path, count) for path, count in sorted(counts.items())
        if path not in budgets
    ]
    offenders += [_describe_gone(path) for path in sorted(budgets) if path not in counts]
    offenders += [
        _describe_over(path, counts[path], budgets[path]) for path in budgeted
        if counts[path] > budgets[path]
    ]
    offenders += [
        _describe_slack(path, counts[path], budgets[path]) for path in budgeted
        if _is_slack(counts[path], budgets[path])
    ]
    return offenders


def _is_slack(count: int, budget: int) -> bool:
    return budget * 100 > count * (100 + SLACK_PERCENT)


def _describe_over(path: str, count: int, budget: int) -> str:
    return f"{path}: {count} words, over its budget of {budget}"


def _describe_slack(path: str, count: int, budget: int) -> str:
    return (
        f"{path}: {count} words, more than {SLACK_PERCENT}% under its budget of {budget} "
        f"— lower the budget to {count}, so the cut stays banked"
    )


def _describe_unbudgeted(path: str, count: int) -> str:
    return f"{path}: {count} words and no budget — add a _WORD_BUDGETS entry for it"


def _describe_gone(path: str) -> str:
    return f"{path}: has a budget but no file — remove its _WORD_BUDGETS entry"
