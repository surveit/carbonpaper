from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from critic.evaluation import DEFAULT_PR_RANGE
from critic.rubric import (
    FROM_ENV_SUFFIX,
    FROM_REPO_SUFFIX,
    NO_RUBRIC_TEXT,
    REPO_ROOT,
    UnsetRubricSourceError,
    load_rubric,
    render_rubric,
    stamp_rubric,
)
from critic.tests.fixture_data import FIXTURES, RUBRICS, STAND_IN_ENVIRON, load_labels
from critic.themes import find_themes_outside, load_theme_vocabulary, select_flag_themes

REPO_CHAIN = ["AGENTS.md", "app/AGENTS.md", "app/runtime/AGENTS.md", "app/templates/AGENTS.md"]
DISTILLED = RUBRICS / "distilled"
PR_URL = re.compile(r"github\.com/surveit/carbonpaper/pull/(\d+)")
THEME_ROW = re.compile(r"^\| ([a-z_]+) \| [\d.]+% \|", re.MULTILINE)


def test_the_empty_rubric_holds_no_rules() -> None:
    rubric = load_rubric(RUBRICS / "none")
    assert rubric.files == []
    assert render_rubric(rubric) == NO_RUBRIC_TEXT


def test_the_instructions_rubric_leads_with_the_owners_file_then_the_repo_chain() -> None:
    files = load_rubric(RUBRICS / "current_instructions", STAND_IN_ENVIRON).files
    assert [file.name for file in files] == [
        "owner-global-CLAUDE.md",
        "repo-AGENTS.md",
        "repo-app-AGENTS.md",
        "repo-app-runtime-AGENTS.md",
        "repo-app-templates-AGENTS.md",
    ]
    assert [file.origin for file in files[1:]] == REPO_CHAIN


def test_the_repo_chain_is_read_live_not_copied() -> None:
    files = load_rubric(RUBRICS / "current_instructions", STAND_IN_ENVIRON).files[1:]
    assert [file.text for file in files] == [(REPO_ROOT / path).read_text(encoding="utf-8") for path in REPO_CHAIN]
    names = [path.name for path in (RUBRICS / "current_instructions").iterdir()]
    assert all(name.endswith((FROM_ENV_SUFFIX, FROM_REPO_SUFFIX)) for name in names)


def test_the_distilled_rubric_renders_its_five_files_in_name_order() -> None:
    rubric = load_rubric(DISTILLED)
    names = [file.name for file in rubric.files]
    assert names == ["00_role.md", "10_themes.md", "20_rules.md", "30_examples.md", "40_not_flagged.md"]
    rendered = render_rubric(rubric)
    positions = [rendered.index(f"--- {name} ---") for name in names]
    assert positions == sorted(positions)


def test_the_distilled_examples_name_training_prs_and_no_held_out_one() -> None:
    held_out_floor, _ = DEFAULT_PR_RANGE
    examples = (DISTILLED / "30_examples.md").read_text(encoding="utf-8")
    example_prs = [int(number) for number in PR_URL.findall(examples)]
    assert len(example_prs) == 48
    every_pr = [int(number) for file in load_rubric(DISTILLED).files for number in PR_URL.findall(file.text)]
    assert [pr for pr in every_pr if pr >= held_out_floor] == []


def test_the_distilled_theme_table_names_the_label_vocabulary() -> None:
    table = (DISTILLED / "10_themes.md").read_text(encoding="utf-8")
    assert set(THEME_ROW.findall(table)) == {theme.slug for theme in load_labels().themes}


def test_a_repo_pointer_that_leaves_the_repo_is_refused(tmp_path: Path) -> None:
    (tmp_path / "outside.md.from-repo").write_text("../../outside.md", encoding="utf-8")
    with pytest.raises(ValueError, match="points outside the repo"):
        load_rubric(tmp_path)


def test_the_owners_file_unset_fails_naming_its_variable() -> None:
    with pytest.raises(UnsetRubricSourceError, match=r"\$CRITIC_OWNER_CLAUDE_MD"):
        load_rubric(RUBRICS / "current_instructions", {})


def test_the_owners_file_at_a_wrong_path_fails(tmp_path: Path) -> None:
    missing = {"CRITIC_OWNER_CLAUDE_MD": str(tmp_path / "CLAUDE.md")}
    with pytest.raises(FileNotFoundError, match="is not a file"):
        load_rubric(RUBRICS / "current_instructions", missing)


def test_a_stamp_records_where_each_file_came_from_and_its_hash() -> None:
    rubric = load_rubric(RUBRICS / "current_instructions", STAND_IN_ENVIRON)
    stamps = stamp_rubric(rubric)
    stand_in = Path(STAND_IN_ENVIRON["CRITIC_OWNER_CLAUDE_MD"])
    expected = hashlib.sha256(stand_in.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert (stamps[0].name, stamps[0].origin, stamps[0].sha256) == ("owner-global-CLAUDE.md", "AGENTS.md", expected)
    # The stand-in is the repo's AGENTS.md, which the second file also reads.
    assert stamps[1].sha256 == expected


def test_themes_outside_the_vocabulary_are_found_once_each() -> None:
    vocabulary = load_labels().themes
    assert find_themes_outside(["naming", "layering", "layering", "praise_or_signoff"], vocabulary) == ["layering"]
    assert find_themes_outside([], vocabulary) == []


def test_a_rubric_file_it_would_not_read_is_refused(tmp_path: Path) -> None:
    (tmp_path / "rules.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="does not read"):
        load_rubric(tmp_path)


def test_a_nested_theme_file_reads_slug_and_definition() -> None:
    themes = {theme.slug: theme.definition for theme in load_labels().themes}
    assert themes["naming"].startswith("A name misdescribes what a value holds")
    assert len(themes) == 33


def test_a_flat_theme_file_reads_and_skips_its_notes() -> None:
    flat = load_theme_vocabulary(FIXTURES / "themes-range_604_788.json")
    noted = load_theme_vocabulary(FIXTURES / "themes-range_1_225.json")
    assert [theme.slug for theme in flat] == ["naming", "layering", "praise"]
    assert [theme.slug for theme in noted] == ["naming", "layering", "praise"]


def test_flag_themes_drop_praise_and_the_agents_own() -> None:
    slugs = [theme.slug for theme in select_flag_themes(load_labels().themes)]
    assert "praise_or_signoff" not in slugs and "question_only" in slugs
    flat = [theme.slug for theme in select_flag_themes(load_theme_vocabulary(FIXTURES / "themes-range_604_788.json"))]
    assert flat == ["naming", "layering"]


def test_a_theme_without_a_definition_is_refused(tmp_path: Path) -> None:
    payload = json.loads((FIXTURES / "themes-range_604_788.json").read_text(encoding="utf-8"))
    payload["naming"] = 36
    broken = tmp_path / "themes.json"
    broken.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="carries no definition"):
        load_theme_vocabulary(broken)
