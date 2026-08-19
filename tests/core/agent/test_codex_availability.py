from __future__ import annotations

import subprocess

import pytest

from app.core.agent import codex_availability


@pytest.mark.parametrize(
    ("platform", "executable", "expected_login", "expected_app_server"),
    [
        (
            "nt",
            r"C:\Program Files\nodejs\codex.cmd",
            (
                r"C:\Windows\System32\cmd.exe",
                "/d",
                "/s",
                "/c",
                "call",
                r"C:\Program Files\nodejs\codex.cmd",
                "login",
                "status",
            ),
            (
                r"C:\Windows\System32\cmd.exe",
                "/d",
                "/s",
                "/c",
                "call",
                r"C:\Program Files\nodejs\codex.cmd",
                "app-server",
                "--stdio",
            ),
        ),
        (
            "nt",
            r"C:\Program Files\Codex\codex.exe",
            (r"C:\Program Files\Codex\codex.exe", "login", "status"),
            (r"C:\Program Files\Codex\codex.exe", "app-server", "--stdio"),
        ),
        (
            "posix",
            "/opt/codex.cmd",
            ("/opt/codex.cmd", "login", "status"),
            ("/opt/codex.cmd", "app-server", "--stdio"),
        ),
    ],
)
def test_backend_returns_the_platform_launch_command(
    monkeypatch,
    platform: str,
    executable: str,
    expected_login: tuple[str, ...],
    expected_app_server: tuple[str, ...],
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        codex_availability, "_IS_WINDOWS", platform == "nt", raising=False
    )
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setattr(codex_availability.shutil, "which", lambda _name: executable)

    def complete_status(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess([], returncode=0)

    monkeypatch.setattr(codex_availability.subprocess, "run", complete_status)

    assert codex_availability.require_codex_backend() == expected_app_server
    assert calls == [expected_login]


def test_windows_shim_requires_a_command_interpreter(monkeypatch) -> None:
    monkeypatch.setattr(codex_availability, "_IS_WINDOWS", True, raising=False)
    monkeypatch.delenv("COMSPEC", raising=False)
    monkeypatch.setattr(
        codex_availability.shutil,
        "which",
        lambda _name: r"C:\Program Files\nodejs\codex.bat",
    )

    with pytest.raises(
        codex_availability.CodexBackendUnavailableError,
        match="Windows command interpreter",
    ):
        codex_availability.require_codex_backend()
