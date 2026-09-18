"""Architecture: `is` against a StageType or RunStatus member is always False."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Literal

from app.core.run_status import RunStatus
from app.models.schema import _Base
from app.models.stages.stage_base import StageType
from arch._helpers import parse_module
from arch.scope import find_source_files_under

_COERCED_ENUMS = frozenset({"StageType", "RunStatus"})
_APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def find_dead_enum_identity_comparisons(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        for node in ast.walk(parse_module(path)):
            if isinstance(node, ast.Compare) and _compares_an_enum_by_identity(node):
                offenders.append(f"{path}:{node.lineno}  {ast.unparse(node)}")
    return sorted(offenders)


def test_no_app_module_compares_a_coerced_enum_by_identity() -> None:
    offenders = find_dead_enum_identity_comparisons(find_source_files_under(_APP_ROOT))
    assert not offenders, (
        f"`_Base` sets use_enum_values=True, so a field typed as one of "
        f"{sorted(_COERCED_ENUMS)} holds a plain string after validation — even when "
        "the input was the member. These branches are never taken, and the lines read "
        "correctly, so no diff shows it. Compare with `==` (both are string enums), or "
        "narrow with isinstance on the stage class:\n  "
        + "\n  ".join(offenders)
    )


def test_a_literal_stage_type_field_holds_a_plain_string() -> None:
    class Tiny(_Base):
        type: Literal[StageType.expand]

    validated = Tiny.model_validate({"type": StageType.expand})
    assert validated.type == StageType.expand
    assert validated.type is not StageType.expand


def test_an_enum_annotated_field_holds_a_plain_string() -> None:
    class Tiny(_Base):
        status: RunStatus

    validated = Tiny.model_validate({"status": RunStatus.OK})
    assert validated.status == RunStatus.OK
    assert not isinstance(validated.status, RunStatus)


def test_the_rule_can_see_a_violation(tmp_path: Path) -> None:
    module = tmp_path / "surface.py"
    module.write_text(
        "def say(stage, manifest):\n"
        "    if stage.type is StageType.expand:\n"
        "        return 'fans out'\n"
        "    if manifest.status is not RunStatus.OK:\n"
        "        return 'unfinished'\n"
        "    if stage.type == StageType.enrich:\n"
        "        return 'one to one'\n"
        "    return stage.status is StageStatus.PENDING\n",
        encoding="utf-8",
    )
    found = [line.rsplit("  ", 1)[1] for line in find_dead_enum_identity_comparisons([module])]
    assert found == [
        "stage.type is StageType.expand",
        "manifest.status is not RunStatus.OK",
    ]


def _compares_an_enum_by_identity(node: ast.Compare) -> bool:
    return any(
        isinstance(op, (ast.Is, ast.IsNot)) and _names_a_coerced_member(side)
        for op, comparator in zip(node.ops, node.comparators, strict=True)
        for side in (node.left, comparator)
    )


def _names_a_coerced_member(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Attribute)
        and isinstance(expression.value, ast.Name)
        and expression.value.id in _COERCED_ENUMS
    )
