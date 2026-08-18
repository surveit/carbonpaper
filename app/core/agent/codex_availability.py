from __future__ import annotations

import os
import shutil
import subprocess

from app.core.agent.errors import CodexBackendUnavailableError


_IS_WINDOWS = os.name == "nt"
_WINDOWS_SHIM_SUFFIXES = {".bat", ".cmd"}


def find_codex_backend_error() -> CodexBackendUnavailableError | None:
    try:
        require_codex_backend()
    except CodexBackendUnavailableError as exc:
        return exc
    return None


def require_codex_backend() -> tuple[str, ...]:
    command = shutil.which("codex")
    if command is None:
        raise CodexBackendUnavailableError(
            "The Codex CLI isn't available. Install it before starting a chat."
        )
    login_status_command = _build_codex_command(command, "login", "status")
    try:
        status = subprocess.run(
            login_status_command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise CodexBackendUnavailableError(
            f"Codex authentication could not be verified: {exc}"
        ) from exc
    if status.returncode != 0:
        raise CodexBackendUnavailableError(
            "Codex isn't authenticated with a ChatGPT subscription. Run `codex login` "
            "before starting a chat."
        )
    return _build_codex_command(command, "app-server", "--stdio")


def _build_codex_command(executable: str, *arguments: str) -> tuple[str, ...]:
    suffix = os.path.splitext(executable)[1].casefold()
    if not _IS_WINDOWS or suffix not in _WINDOWS_SHIM_SUFFIXES:
        return (executable, *arguments)
    interpreter = os.environ.get("COMSPEC")
    if interpreter is None:
        raise CodexBackendUnavailableError(
            "The Windows command interpreter isn't available to start the Codex CLI."
        )
    return (interpreter, "/d", "/s", "/c", "call", executable, *arguments)
