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
"""
from __future__ import annotations

from pathlib import Path

from arch import check_no_dict_keys

_CONNECTOR_PARAM_KEYS = {"path", "format", "file", "list_columns", "parse_dates"}


def test_runner_never_touches_connector_param_keys() -> None:
    runner = Path(__file__).resolve().parents[1] / "runner.py"
    offenders = check_no_dict_keys([runner], _CONNECTOR_PARAM_KEYS)
    assert not offenders, (
        "runner.py must stay generic over connector params; these keys belong "
        "to the stage modules (stages/input_data.py) and the Connector model:\n  "
        + "\n  ".join(offenders)
    )
