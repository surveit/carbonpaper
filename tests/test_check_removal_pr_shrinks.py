from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.check_removal_pr_shrinks import (
    LIFTING_LABEL,
    check_removal_pr_shrinks,
    find_removal_verb,
    main,
)

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "shrink-intent.yml"

# Every merged PR through #1096 that the gate would have failed.
HISTORICAL_FIRINGS = [
    (803, "Retire the silent picks in a collapse: an aggregation's `where`, and `first`", 607, 26),
    (573, "fix(migrations): retire publish.template, which b25f4f83 left stored", 252, 1),
    (867, "Drop a hidden project in one place, not at each listing", 268, 39),
    (1061, "Drop the challenges that repeat another, and say what the run already guarantees", 229, 16),
    (890, "Merge the path chips down and set them as text", 157, 28),
    (764, "Merge plural noun/field counts into the singular in the lexicon scan", 65, 0),
    (845, "Delete the two-grain refusal no run can reach", 106, 63),
    (363, "refactor(models): cut the re-export bookkeeping in app/models/__init__.py", 393, 360),
    (493, "Drop the publish gate from a run", 265, 239),
    (359, "Remove create_version_from_disk; versioning stops reading the working copy", 140, 131),
]
# A migration that retires stored data, and a "Drop" that names a product feature.
GREW_ON_PURPOSE = {573, 1061}

HISTORICAL_PASSES = [
    (1076, "Retire the working copy: a project's stages are its newest version", 535, 1025),
    (907, 'Drop the "First:" off the chatbot offer', 1, 1),
    (692, "chore(lineage): drop the EdgeKind member nothing constructs", 0, 3),
    (339, "docs(stage tests prompt): halve the added prose to what teaches", 12, 24),
]


@pytest.mark.parametrize(("pr", "title", "additions", "deletions"), HISTORICAL_FIRINGS)
def test_a_historical_firing_fails_naming_the_verb_the_counts_and_the_label(
    pr: int, title: str, additions: int, deletions: int
) -> None:
    failure = check_removal_pr_shrinks(title, additions, deletions)
    assert failure is not None, f"#{pr} grew under a removal title and must fail"
    assert f"'{find_removal_verb(title)}'" in failure
    assert f"adds {additions} lines and deletes {deletions}" in failure
    assert f"a human sets the label '{LIFTING_LABEL}'" in failure


@pytest.mark.parametrize(("pr", "title", "additions", "deletions"), HISTORICAL_PASSES)
def test_a_removal_title_that_does_not_grow_passes(
    pr: int, title: str, additions: int, deletions: int
) -> None:
    assert check_removal_pr_shrinks(title, additions, deletions) is None, f"#{pr} did not grow"


def test_the_prs_that_grew_on_purpose_fail_the_title_check_and_need_the_label() -> None:
    firings = {pr: (title, additions, deletions) for pr, title, additions, deletions in HISTORICAL_FIRINGS}
    for pr in GREW_ON_PURPOSE:
        assert check_removal_pr_shrinks(*firings[pr]) is not None, f"#{pr} passes on its title alone"
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["shrink-intent"]
    assert LIFTING_LABEL in job["if"], f"{WORKFLOW.name} must skip the job when '{LIFTING_LABEL}' is set"


@pytest.mark.parametrize(
    ("title", "verb"),
    [
        ("Drop the publish gate from a run", "drop"),
        ("refactor(models): cut the re-export bookkeeping", "cut"),
        ("fix!: remove the flag", "remove"),
        ("Dedupe the stage list", "dedupe"),
        ("Dropping the gate", None),
        ("Add a check that the diff shrinks", None),
        ("", None),
    ],
)
def test_find_removal_verb_reads_the_first_word_after_a_type_scope_prefix(
    title: str, verb: str | None
) -> None:
    assert find_removal_verb(title) == verb


def test_a_title_that_names_no_removal_may_grow() -> None:
    assert check_removal_pr_shrinks("Add a check that the diff shrinks", 300, 0) is None


def test_main_exits_one_on_a_firing_and_zero_on_a_pass(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--title", "Drop the publish gate from a run", "--additions", "265", "--deletions", "239"]) == 1
    assert LIFTING_LABEL in capsys.readouterr().err
    assert main(["--title=-leading dash", "--additions", "1", "--deletions", "0"]) == 0
