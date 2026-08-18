from __future__ import annotations


class CodexProtocolError(Exception):
    pass


class CodexBackendUnavailableError(RuntimeError):
    pass
