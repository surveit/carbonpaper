"""Invariant 2 (first pass) of issue #100: authored python stage code runs in a
subprocess with a scrubbed environment and a wall-clock timeout.

- A stage that reads ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_* sees them absent.
- A stage that runs forever is killed by the timeout.
- The existing function contract (inputs/outputs, error types) is preserved.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.models import Stage
from app.runtime.stages import handle_python_frame_function, handle_python_row_function
from app.runtime.stages._sandbox import is_secret_env, scrubbed_env


def _row_stage(code: str) -> Stage:
    return Stage.model_validate({
        "id": "t", "name": "t", "type": "python_row_function",
        "inputs": [{"id": "src"}],
        "function": {"kind": "inline", "code": code},
    })


def _frame_stage(code: str) -> Stage:
    return Stage.model_validate({
        "id": "t", "name": "t", "type": "python_frame_function",
        "inputs": [{"id": "src"}],
        "function": {"kind": "inline", "code": code},
    })


# ── env scrubbing (unit) ──────────────────────────────────────────────────────

def test_is_secret_env_flags_credentials():
    for name in (
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_EXPIRES",
        "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "GITHUB_TOKEN", "MY_DB_PASSWORD", "SOME_CREDENTIAL",
    ):
        assert is_secret_env(name), f"{name} should be treated as secret"


def test_is_secret_env_keeps_benign_vars():
    for name in ("PATH", "HOME", "LANG", "PWD", "CW_LLM_BACKEND", "TERM"):
        assert not is_secret_env(name), f"{name} should be preserved"


def test_scrubbed_env_removes_secrets_keeps_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    env = scrubbed_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "PATH" in env  # benign vars survive so the child can run


# ── end-to-end: the secret is absent inside the executed stage ────────────────

def test_stage_cannot_read_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-must-not-leak")
    code = (
        "import os\n"
        "def transform(row):\n"
        "    return {\n"
        "        'api_key': os.environ.get('ANTHROPIC_API_KEY', ''),\n"
        "        'oauth': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''),\n"
        "        'has_path': bool(os.environ.get('PATH')),\n"
        "    }\n"
    )
    out = handle_python_row_function(_row_stage(code), {"src": pd.DataFrame({"x": [1]})}, {})
    assert out.loc[0, "api_key"] == ""      # secret scrubbed → empty
    assert out.loc[0, "oauth"] == ""
    assert bool(out.loc[0, "has_path"]) is True  # benign env still present


def test_stage_key_lookup_raises_keyerror_not_leak(monkeypatch):
    # os.environ["ANTHROPIC_API_KEY"] (no default) must KeyError inside the child,
    # proving the var is truly absent — and that error propagates as a KeyError.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    code = (
        "import os\n"
        "def transform(row):\n"
        "    return {'k': os.environ['ANTHROPIC_API_KEY']}\n"
    )
    with pytest.raises(KeyError):
        handle_python_row_function(_row_stage(code), {"src": pd.DataFrame({"x": [1]})}, {})


# ── timeout ───────────────────────────────────────────────────────────────────

def test_runaway_stage_is_killed(monkeypatch):
    monkeypatch.setenv("CW_STAGE_EXEC_TIMEOUT_S", "2")
    code = (
        "def transform(row):\n"
        "    while True:\n"
        "        pass\n"
    )
    with pytest.raises(TimeoutError):
        handle_python_row_function(_row_stage(code), {"src": pd.DataFrame({"x": [1]})}, {})


def test_runaway_frame_stage_is_killed(monkeypatch):
    monkeypatch.setenv("CW_STAGE_EXEC_TIMEOUT_S", "2")
    code = (
        "def transform(df):\n"
        "    while True:\n"
        "        pass\n"
    )
    with pytest.raises(TimeoutError):
        handle_python_frame_function(_frame_stage(code), {"src": pd.DataFrame({"x": [1]})}, {})


# ── contract preserved ────────────────────────────────────────────────────────

def test_row_function_still_transforms():
    code = "def transform(row):\n    return {'x': row['x'], 'y': row['x'] * 10}\n"
    out = handle_python_row_function(_row_stage(code), {"src": pd.DataFrame({"x": [1, 2, 3]})}, {})
    assert list(out["y"]) == [10, 20, 30]


def test_frame_function_still_transforms():
    code = "def transform(df):\n    df = df.copy()\n    df['y'] = df['x'] * 2\n    return df\n"
    out = handle_python_frame_function(_frame_stage(code), {"src": pd.DataFrame({"x": [1, 2]})}, {})
    assert list(out["y"]) == [2, 4]


def test_user_code_error_propagates_as_valueerror():
    code = "def transform(row):\n    raise ValueError('boom')\n"
    with pytest.raises(ValueError, match="boom"):
        handle_python_row_function(_row_stage(code), {"src": pd.DataFrame({"x": [1]})}, {})
