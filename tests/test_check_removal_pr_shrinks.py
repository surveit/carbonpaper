from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.check_removal_pr_shrinks import (
    GROWS_ON_PURPOSE_LABEL,
    check_removal_pr_shrinks,
    count_non_test_lines,
    find_removal_verb,
    is_test_path,
    main,
    read_pr_files,
)

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "shrink-intent.yml"

# Every merged PR through #1096 the gate fails, with its additions and deletions outside test files.
HISTORICAL_FIRINGS = [
    (803, "Retire the silent picks in a collapse: an aggregation's `where`, and `first`", 290, 26),
    (573, "fix(migrations): retire publish.template, which b25f4f83 left stored", 120, 1),
    (867, "Drop a hidden project in one place, not at each listing", 43, 22),
    (1061, "Drop the challenges that repeat another, and say what the run already guarantees", 179, 10),
    (890, "Merge the path chips down and set them as text", 132, 27),
    (764, "Merge plural noun/field counts into the singular in the lexicon scan", 23, 0),
    (363, "refactor(models): cut the re-export bookkeeping in app/models/__init__.py", 379, 347),
]
# A migration that retires stored data, and a "Drop" that names a product feature.
GREW_ON_PURPOSE = {573, 1061}

HISTORICAL_PASSES = [
    (845, "Delete the two-grain refusal no run can reach", 47, 63),
    (493, "Drop the publish gate from a run", 116, 133),
    (359, "Remove create_version_from_disk; versioning stops reading the working copy", 42, 44),
    (1076, "Retire the working copy: a project's stages are its newest version", 192, 380),
    (907, 'Drop the "First:" off the chatbot offer', 1, 1),
    (692, "chore(lineage): drop the EdgeKind member nothing constructs", 0, 3),
    (339, "docs(stage tests prompt): halve the added prose to what teaches", 12, 24),
]

PR_845_FILES = [
    ("app/models/branch_analysis.py", 2, 0),
    ("app/runtime/errors.py", 0, 4),
    ("app/services/scope.py", 18, 21),
    ("app/static/run-status.css", 1, 0),
    ("app/templates/eval_run.html", 1, 1),
    ("app/templates/scope_map.html", 6, 0),
    ("app/templates/scope_refused.html", 0, 15),
    ("app/web/routers/scope.py", 1, 11),
    ("app/web/scope_view.py", 14, 7),
    ("docs/branch-analysis.md", 4, 4),
    ("tests/scope_fixture.py", 37, 0),
    ("tests/test_scope_page.py", 22, 0),
]


def _pages(files: list[tuple[str, int, int]]) -> list[list[dict[str, object]]]:
    return [[{"filename": path, "additions": added, "deletions": deleted} for path, added, deleted in files]]


@pytest.mark.parametrize(("pr", "title", "additions", "deletions"), HISTORICAL_FIRINGS)
def test_a_historical_firing_fails_naming_the_verb_the_counts_and_the_label(
    pr: int, title: str, additions: int, deletions: int
) -> None:
    failure = check_removal_pr_shrinks(title, additions, deletions)
    assert failure is not None, f"#{pr} grew outside tests under a removal title and must fail"
    assert f"'{find_removal_verb(title)}'" in failure
    assert f"adds {additions} lines and deletes {deletions}" in failure
    assert f"a human sets the label '{GROWS_ON_PURPOSE_LABEL}'" in failure


@pytest.mark.parametrize(("pr", "title", "additions", "deletions"), HISTORICAL_PASSES)
def test_a_removal_title_whose_non_test_lines_do_not_grow_passes(
    pr: int, title: str, additions: int, deletions: int
) -> None:
    assert check_removal_pr_shrinks(title, additions, deletions) is None, f"#{pr} did not grow outside tests"


def test_a_removal_that_grows_only_by_its_tests_passes() -> None:
    title = "Delete the two-grain refusal no run can reach"
    files = read_pr_files(_pages(PR_845_FILES))
    whole_diff = (sum(file.additions for file in files), sum(file.deletions for file in files))
    assert check_removal_pr_shrinks(title, *whole_diff) is not None
    assert count_non_test_lines(files) == (47, 63)
    assert check_removal_pr_shrinks(title, *count_non_test_lines(files)) is None


def test_the_prs_that_grew_on_purpose_fail_the_title_check_and_need_the_label() -> None:
    firings = {pr: (title, additions, deletions) for pr, title, additions, deletions in HISTORICAL_FIRINGS}
    for pr in GREW_ON_PURPOSE:
        assert check_removal_pr_shrinks(*firings[pr]) is not None, f"#{pr} passes on its title alone"
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["shrink-intent"]
    skip_when_labelled = f"${{{{ !contains(github.event.pull_request.labels.*.name, '{GROWS_ON_PURPOSE_LABEL}') }}}}"
    assert job["if"] == skip_when_labelled


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("tests/test_scope_page.py", True),
        ("tests/fixtures/sample.csv", True),
        ("critic/tests/test_cli.py", True),
        ("app/runtime/_arch_tests/test_stages_no_cross_run_disk.py", True),
        ("app/conftest.py", True),
        ("app/test_helpers.py", True),
        ("app/services/scope.py", False),
        ("docs/testing.md", False),
        ("app/contests.py", False),
    ],
)
def test_is_test_path_names_test_dirs_test_modules_and_conftest(path: str, is_test: bool) -> None:
    assert is_test_path(path) is is_test


@pytest.mark.parametrize(
    ("title", "verb"),
    [
        ("Drop the publish gate from a run", "drop"),
        ("refactor(models): cut the re-export bookkeeping", "cut"),
        ("fix!: remove the flag", "remove"),
        ("R03: Remove the flag", "remove"),
        ("T08-fix: remove the flag", "remove"),
        ("[WIP] Remove the flag", "remove"),
        ("  Drop the flag  ", "drop"),
        ("Dedupe the stage list", "dedupe"),
        ("Drop-in replacement for the loader", None),
        ("Dropping the gate", None),
        ("Add a check that the diff shrinks", None),
        ("", None),
    ],
)
def test_find_removal_verb_reads_the_first_word_after_a_title_prefix(title: str, verb: str | None) -> None:
    assert find_removal_verb(title) == verb


def test_a_title_that_names_no_removal_may_grow() -> None:
    assert check_removal_pr_shrinks("Add a check that the diff shrinks", 300, 0) is None


def test_main_counts_the_files_listing_and_exits_one_on_a_firing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_files = tmp_path / "pr-files.json"
    pr_files.write_text(json.dumps(_pages([("app/x.py", 5, 1), ("tests/test_x.py", 0, 90)])), encoding="utf-8")
    assert main(["--title", "Drop the gate", "--pr-files", str(pr_files)]) == 1
    assert "adds 5 lines and deletes 1" in capsys.readouterr().err
    assert main(["--title=-leading dash", "--pr-files", str(pr_files)]) == 0


def test_main_fails_loudly_on_a_files_listing_it_cannot_read(tmp_path: Path) -> None:
    pr_files = tmp_path / "pr-files.json"
    pr_files.write_text(json.dumps([[{"filename": "app/x.py"}]]), encoding="utf-8")
    with pytest.raises(KeyError):
        main(["--title", "Drop the gate", "--pr-files", str(pr_files)])
