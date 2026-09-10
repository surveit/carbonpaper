"""The prompt dump (scripts/dump_prompts.py) covers every prompt this app ships and
every tool offered beside it — a new surface that never reaches the dump fails here,
because a dump that silently omits one reads as "this is all of it"."""
from __future__ import annotations

import ast
import re
from importlib import import_module
from pathlib import Path
from typing import Iterator

import pytest


from app.compiler.claim_attack.attackers import ATTACKERS
from app.core.paths import repo_root
from scripts.dump_prompts import render_prompt_dump

# A module-level string constant whose name ends in one of these is a prompt shipped
# to a model. The dump has to contain its text.
_PROMPT_NAME_SUFFIXES = ("SYSTEM_PROMPT", "INSTRUCTIONS")


@pytest.fixture(scope="module")
def dump() -> str:
    return render_prompt_dump()


def test_dump_contains_every_shipped_prompt(dump: str) -> None:
    flattened = _one_line(dump)
    missing = [
        f"{path.relative_to(repo_root())}::{name}"
        for path, name, text in find_prompt_constants()
        if text not in flattened
    ]
    assert not missing, (
        "prompt text that never reaches the dump — add its surface to "
        "scripts/dump_prompts.py:render_prompt_dump:\n  " + "\n  ".join(missing)
    )


def test_dump_offers_the_editing_agent_every_tool_it_binds(dump: str) -> None:
    from app.tools.editing import EditingContext, build_editing_tools

    bound = build_editing_tools(EditingContext(project_id="p", base_url="http://reader.test/"))
    missing = [spec.name for spec in bound if f"#### `{spec.name}`" not in dump]
    assert not missing, f"editing tools absent from the dump: {missing}"


def test_dump_carries_every_claim_attacker_and_the_turn_that_merges_them(dump: str) -> None:
    for attacker in ATTACKERS:
        assert f"## Claim attacker · {attacker.value}" in dump
    assert "## Claim attack · orchestrator" in dump


def test_each_claim_attacker_answers_in_its_own_schema(dump: str) -> None:
    grounding = read_section(dump, "Claim attacker · grounding")
    meaning = read_section(dump, "Claim attacker · meaning")

    assert '"phrases"' in grounding and '"rewrites"' not in grounding
    assert '"rewrites"' in meaning and '"phrases"' not in meaning


def read_section(dump: str, title: str) -> str:
    heading = "\n## "
    return dump.split(f"{heading}{title}\n")[1].split(heading)[0]


def test_dump_states_what_it_leaves_out(dump: str) -> None:
    assert "the per-run task (the user message)" in dump
    assert "built per stage" in dump


def find_prompt_constants() -> list[tuple[Path, str, str]]:
    found = []
    for path in sorted(repo_root().glob("app/**/*.py")):
        for name in _module_level_names(path):
            if not name.endswith(_PROMPT_NAME_SUFFIXES):
                continue
            value = getattr(import_module(_module_name(path)), name, None)
            if isinstance(value, str) and value.strip():
                found.append((path, name, _one_line(value)))
    return found


def _module_level_names(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                yield target.id


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(repo_root()).with_suffix("").parts)


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_the_constant_scan_finds_the_known_surfaces() -> None:
    # Without this, a scan matching nothing would pass the test above vacuously.
    names = {name for _path, name, _text in find_prompt_constants()}
    assert {"EDITING_SYSTEM_PROMPT", "INSTRUCTIONS", "DATA_MODEL_SYSTEM_PROMPT",
            "REVIEW_GUIDE_SYSTEM_PROMPT", "STAGE_TESTS_SYSTEM_PROMPT",
            "SYSTEM_PROMPT", "GROUNDING_SYSTEM_PROMPT", "DATA_DEFECTS_SYSTEM_PROMPT",
            "CHOICES_SYSTEM_PROMPT", "OMISSIONS_SYSTEM_PROMPT", "COVERAGE_SYSTEM_PROMPT",
            "MEANING_SYSTEM_PROMPT", "ORCHESTRATOR_SYSTEM_PROMPT"} <= names
