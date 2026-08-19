# Task 5 report

Scope:

- Added one fake-server mode that emits `remoteControl/status/changed` before a valid `submit_answer`.
- Added one regression test for that production break.
- Added one explicit notification branch that ignores only `remoteControl/status/changed`.
- Left all other unknown notifications failing loudly.
- Left unsupported server requests, including MCP requests, explicitly rejected.

RED:

Command:

`& 'C:\journalism_sprint\prototype_one\.venv\Scripts\python.exe' -m pytest tests\runtime\test_codex_transform.py -k remote_control_status_notification_is_ignored_before_submit_answer -vv --basetemp .pytest-tmp-task5-red`

Output:

```text
tests/runtime/test_codex_transform.py::test_remote_control_status_notification_is_ignored_before_submit_answer FAILED
E   app.core.agent.errors.CodexProtocolError: unsupported Codex notification: remoteControl/status/changed
```

This shows the authenticated lifecycle notification arrived before `submit_answer`.
It assumes the fake server matches the observed ordering.
It does not prove any other notification is safe to ignore.

GREEN:

Command:

`& 'C:\journalism_sprint\prototype_one\.venv\Scripts\python.exe' -m pytest tests\runtime\test_codex_transform.py -vv --basetemp .pytest-tmp-task5-green`

Output:

```text
============================== 8 passed in 0.96s ==============================
```

Static checks:

Command:

`& 'C:\journalism_sprint\prototype_one\.venv\Scripts\ruff.exe' check app\runtime\codex_transform.py tests\runtime\test_codex_transform.py`

Output:

```text
All checks passed!
```

Command:

`& 'C:\journalism_sprint\prototype_one\.venv\Scripts\mypy.exe' app\runtime\codex_transform.py tests\runtime\test_codex_transform.py`

Output:

```text
Success: no issues found in 2 source files
```

Self-review:

- The runtime change is one named constant and one named branch.
- The test fixture now models the exact notification that broke the authenticated smoke.
- No wildcard ignore was introduced.
- MCP refusal behavior and all other unsupported notifications still follow the existing fail-loud path.

Commit:

- `86b6a0a3` (`fix: ignore Codex lifecycle notification`)
