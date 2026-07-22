"""Architecture: the runner attaches no meaning to connector params.

``app/runtime/runner.py`` orchestrates runs generically: it merges run bindings
into connector params and asks each stage type's preflight whether the stage is
ready — without knowing what any param means. The param vocabulary ("path",
"format", "file", "list_columns", "parse_dates") belongs to the stage modules
that read it (``stages/input_data.py``) and to the Connector model that
validates it. If the runner starts touching those keys, stage-specific
semantics are leaking back into the orchestrator — the exact smell this rule
exists to stop. Scope is runner.py alone: stage modules under ``stages/``
legitimately own these keys.

Matching is by exact dict-key spelling, not by word segment: "path" flags a
literal ``"path"`` key but never a compound like ``"output_path"`` or
``"queue_path"`` — the runner's own manifest/queue bookkeeping keys, unrelated
to connector-param semantics. ``check_no_dict_keys`` inspects only dict keys
(a subscript, a ``.get`` first argument, or a dict-literal key), never plain
identifiers — this file also imports ``pathlib.Path`` and uses local
path-bookkeeping variable names for its own manifest-writing, none of which
this rule is concerned with.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch import check_no_dict_keys
from arch._helpers import find_dict_key_uses

_CONNECTOR_PARAM_KEYS = {"path", "format", "file", "list_columns", "parse_dates"}


def test_runner_never_touches_connector_param_keys() -> None:
    runner = Path(__file__).resolve().parents[1] / "runner.py"
    offenders = check_no_dict_keys([runner], _CONNECTOR_PARAM_KEYS)
    assert not offenders, (
        "runner.py must stay generic over connector params; these keys belong "
        "to the stage modules (stages/input_data.py) and the Connector model:\n  "
        + "\n  ".join(offenders)
    )


# --- unit tests for the exact dict-key match, on inline snippets ----------


def test_find_dict_key_uses_flags_the_literal_path_subscript_key() -> None:
    tree = ast.parse('value = params["path"]\n')
    assert find_dict_key_uses(tree, {"path"}) == [(1, "path")]


def test_find_dict_key_uses_flags_the_literal_path_get_call_key() -> None:
    tree = ast.parse('params.get("path")\n')
    assert find_dict_key_uses(tree, {"path"}) == [(1, "path")]


def test_find_dict_key_uses_flags_the_literal_path_dict_literal_key() -> None:
    tree = ast.parse('d = {"path": "/tmp/x"}\n')
    assert find_dict_key_uses(tree, {"path"}) == [(1, "path")]


def test_find_dict_key_uses_ignores_the_compound_output_path_key() -> None:
    tree = ast.parse('value = params["output_path"]\n')
    assert find_dict_key_uses(tree, {"path"}) == []


def test_find_dict_key_uses_ignores_the_compound_queue_path_key() -> None:
    tree = ast.parse('value = params["queue_path"]\n')
    assert find_dict_key_uses(tree, {"path"}) == []
