from __future__ import annotations

import shutil
import subprocess

from app.core.agent.errors import CodexBackendUnavailableError


def find_codex_backend_error() -> CodexBackendUnavailableError | None:
    command = shutil.which("codex")
    if command is None:
        return CodexBackendUnavailableError(
            "The Codex CLI isn't available. Install it before starting a chat."
        )
    try:
        status = subprocess.run(
            (command, "login", "status"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return CodexBackendUnavailableError(
            f"Codex authentication could not be verified: {exc}"
        )
    if status.returncode != 0:
        return CodexBackendUnavailableError(
            "Codex isn't authenticated with a ChatGPT subscription. Run `codex login` "
            "before starting a chat."
        )
    return None


def require_codex_backend() -> None:
    error = find_codex_backend_error()
    if error is not None:
        raise error
