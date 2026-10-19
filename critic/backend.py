from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from critic.records import CriticRecord, ForeignRecord, JsonSchemaDocument, UncheckedModelAnswer

CLAUDE_EXECUTABLE = "claude"
DEFAULT_TIMEOUT_SECONDS = 1200.0


class BackendUnavailableError(RuntimeError):
    pass


class BackendReplyError(RuntimeError):
    pass


class ModelRequest(CriticRecord):
    system: str
    user: str
    answer_schema: JsonSchemaDocument


class ModelReply(CriticRecord):
    answer: UncheckedModelAnswer
    cost_usd: float
    model_ids: list[str]
    duration_ms: int


class ModelBackend(Protocol):
    def validate_available(self) -> None: ...

    def ask(self, request: ModelRequest) -> ModelReply: ...


class _AuthStatus(ForeignRecord):
    loggedIn: bool


class _CliResult(ForeignRecord):
    is_error: bool
    result: str | None = None
    structured_output: UncheckedModelAnswer | None = None
    total_cost_usd: float
    duration_ms: int
    modelUsage: dict[str, object]


class ClaudeCliBackend:
    """Headless `claude -p`: safe mode keeps every CLAUDE.md out, and no tool can read the repo."""

    def __init__(self, model: str | None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds

    def validate_available(self) -> None:
        completed = _run([_find_executable(), "auth", "status"], stdin="", cwd=None, timeout=60.0)
        try:
            status = _AuthStatus.model_validate_json(completed.stdout)
        except ValidationError as error:
            raise BackendUnavailableError(
                f"`claude auth status` exited {completed.returncode}: {completed.stderr.strip()}"
            ) from error
        if not status.loggedIn:
            raise BackendUnavailableError("claude is not logged in: run `claude auth login`")

    def ask(self, request: ModelRequest) -> ModelReply:
        with tempfile.TemporaryDirectory(prefix="critic-") as scratch:
            system_file = Path(scratch) / "system.txt"
            system_file.write_text(request.system, encoding="utf-8")
            command = self._build_command(system_file, request.answer_schema)
            completed = _run(command, stdin=request.user, cwd=scratch, timeout=self.timeout_seconds)
        return parse_cli_result(completed.stdout, completed.stderr)

    def _build_command(self, system_file: Path, schema: JsonSchemaDocument) -> list[str]:
        command = [
            _find_executable(), "-p", "--safe-mode", "--tools", "", "--no-session-persistence",
            "--output-format", "json", "--system-prompt-file", str(system_file),
            "--json-schema", json.dumps(schema),
        ]
        return command if self.model is None else [*command, "--model", self.model]


def parse_cli_result(stdout: str, stderr: str) -> ModelReply:
    try:
        result = _CliResult.model_validate_json(stdout)
    except ValidationError as error:
        raise BackendReplyError(f"claude printed no result JSON; stderr: {stderr.strip()}") from error
    if result.is_error:
        raise BackendReplyError(f"claude reported an error: {result.result}")
    if result.structured_output is None:
        raise BackendReplyError(f"claude returned no structured answer: {result.result}")
    return ModelReply(
        answer=result.structured_output,
        cost_usd=result.total_cost_usd,
        model_ids=sorted(result.modelUsage),
        duration_ms=result.duration_ms,
    )


def _find_executable() -> str:
    executable = shutil.which(CLAUDE_EXECUTABLE)
    if executable is None:
        raise BackendUnavailableError(f"the `{CLAUDE_EXECUTABLE}` CLI is not on PATH")
    return executable


def _run(
    command: list[str], stdin: str, cwd: str | None, timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, input=stdin, capture_output=True, text=True, encoding="utf-8",
            cwd=cwd, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise BackendReplyError(f"claude did not answer within {timeout:.0f}s") from error
