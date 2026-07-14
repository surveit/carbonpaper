"""Run authored stage code in a scrubbed-env subprocess with a wall-clock timeout.

Invariant 2 (first pass) of issue #100: agent-/human-authored python stage code
(`python_row_function` / `python_frame_function`) must not execute in the runner's
own process with the runner's own environment. This module runs it in a child
`python` process whose environment has every secret-looking variable removed
(`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_*`, and anything whose name looks like a
key/token/secret) and which is killed if it exceeds a wall-clock budget. So a
malicious or runaway stage can neither read the operator's API credentials nor
hang the runner forever.

This is DEFENCE IN DEPTH, a deliberate first pass — NOT a real sandbox. There is
no filesystem or network isolation here (a real container tier is deferred, see
#100). It is cross-platform on purpose: plain `subprocess` + `env=` + `timeout`,
no Linux-only seccomp/landlock.

The module is dual-use: imported, it exposes `run_authored_function` (the parent
side); run as a script (`python _sandbox.py <in> <out>`) it IS the child worker.
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import tempfile
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Secret-env scrubbing
# ─────────────────────────────────────────────────────────────────────────────
# A variable is dropped if its name contains a secret-ish substring OR starts
# with a known-credential prefix. Substrings catch the common providers
# (AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, OPENAI_API_KEY, ...); the prefixes catch
# the ones that don't self-describe (ANTHROPIC_BASE_URL, CLAUDE_CODE_OAUTH_*).
_SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "CREDENTIAL")
_SECRET_PREFIXES = (
    "ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_", "OPENAI_", "AWS_", "AZURE_",
    "GCP_", "GOOGLE_", "GITHUB_", "GH_", "HF_", "HUGGINGFACE_",
)


def is_secret_env(name: str) -> bool:
    """True if an env-var name looks like it carries a secret/credential."""
    upper = name.upper()
    if any(sub in upper for sub in _SECRET_SUBSTRINGS):
        return True
    return any(upper.startswith(pre) for pre in _SECRET_PREFIXES)


def scrubbed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy `base` (default: the current environment) with every secret-looking
    variable removed. Non-secret vars (PATH, HOME, LANG, ...) are preserved so the
    child can still find its interpreter and locale."""
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if not is_secret_env(k)}


def _timeout_s() -> float:
    """Wall-clock budget for one stage-code execution. Read at call time so tests
    (and operators) can tighten it via `CW_STAGE_EXEC_TIMEOUT_S`."""
    return float(os.environ.get("CW_STAGE_EXEC_TIMEOUT_S", "60"))


# ─────────────────────────────────────────────────────────────────────────────
# Callable resolution (shared by the worker; also reused for the in-process
# publish path in python_functions.py)
# ─────────────────────────────────────────────────────────────────────────────

def resolve_callable(kind: str, code: str | None, module: str | None, fn_name: str) -> Any:
    """Resolve the stage callable from its spec. `kind` is "inline" or "module".

    For inline code this runs `exec` — in the WORKER process that is the scrubbed,
    timed child; the parent never execs authored code. Kept identical to the
    original loader so behaviour (and error messages) are unchanged."""
    if kind == "module":
        import importlib

        if not module:
            raise ValueError("function.kind=module without module")
        mod = importlib.import_module(module)
        return getattr(mod, fn_name)
    if kind == "inline":
        ns: dict[str, Any] = {}
        exec(code or "", ns)  # noqa: S102 — the whole point: run authored code (in the sandboxed child)
        fn = ns.get(fn_name) or ns.get("transform")
        if fn is None:
            raise ValueError(f"Inline function '{fn_name or 'transform'}' not defined")
        return fn
    raise ValueError(f"Unknown function kind: {kind}")


# ─────────────────────────────────────────────────────────────────────────────
# Parent side: spawn the child, feed it the payload, read the result
# ─────────────────────────────────────────────────────────────────────────────

class SandboxError(RuntimeError):
    """The stage-code subprocess failed for an infrastructural reason (crash, no
    result written, unpicklable payload). Distinct from an exception raised BY the
    authored code, which is reconstructed and re-raised as its original type."""


def _reraise_worker_error(err_type: str, message: str) -> None:
    """Re-raise an error the worker reported. If the worker's exception type is a
    builtin (ValueError, KeyError, ...), re-raise that exact type so the stage
    contract is preserved (e.g. row functions still raise ValueError); otherwise
    surface it as a SandboxError."""
    import builtins

    cls = getattr(builtins, err_type, None)
    if isinstance(cls, type) and issubclass(cls, BaseException) and cls is not BaseException:
        raise cls(message)
    raise SandboxError(f"{err_type}: {message}")


def run_authored_function(
    *,
    kind: str,
    code: str | None,
    module: str | None,
    fn_name: str,
    mode: str,
    frames: list[Any] | None = None,
    records: list[dict[Any, Any]] | None = None,
    timeout_s: float | None = None,
) -> Any:
    """Run authored stage code in a scrubbed-env, timed subprocess.

    `mode` is "frame" (call `fn(*frames)` once, return its result) or "row" (call
    `fn(record)` for each record, return the list of raw results — the caller
    validates each is a dict). Raises `TimeoutError` if the child exceeds the
    wall-clock budget, `SandboxError` on infrastructural failure, or the authored
    code's own exception type on a user-code error."""
    budget = _timeout_s() if timeout_s is None else timeout_s
    payload = {
        "kind": kind,
        "code": code,
        "module": module,
        "fn_name": fn_name,
        "mode": mode,
        "frames": frames,
        "records": records,
        # Replicate the parent's import path so `kind=module` (and any imports the
        # authored code makes) resolve exactly as they would in-process.
        "sys_path": list(sys.path),
    }

    in_fd, in_path = tempfile.mkstemp(prefix="cw_stage_in_", suffix=".pkl")
    out_fd, out_path = tempfile.mkstemp(prefix="cw_stage_out_", suffix=".pkl")
    os.close(out_fd)
    try:
        with os.fdopen(in_fd, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)

        cmd = [sys.executable, os.path.abspath(__file__), in_path, out_path]
        try:
            proc = subprocess.run(
                cmd,
                env=scrubbed_env(),
                capture_output=True,
                timeout=budget,
                # New session so a timeout kill can, on POSIX, take out any
                # grandchildren the stage code spawned, not just the worker.
                start_new_session=(os.name == "posix"),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"stage code exceeded the {budget:g}s execution budget and was killed "
                f"(set CW_STAGE_EXEC_TIMEOUT_S to change it)"
            ) from exc

        try:
            with open(out_path, "rb") as fh:
                result = pickle.load(fh)
        except (EOFError, pickle.UnpicklingError, OSError) as exc:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            raise SandboxError(
                f"stage code subprocess produced no result (exit {proc.returncode}). "
                f"stderr:\n{stderr}"
            ) from exc

        if not result.get("ok"):
            _reraise_worker_error(result.get("error_type", "SandboxError"), result.get("error", ""))
        return result.get("value")
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Child side: read payload, run the authored code, write the result
# ─────────────────────────────────────────────────────────────────────────────

def _worker_main(in_path: str, out_path: str) -> int:
    with open(in_path, "rb") as fh:
        payload = pickle.load(fh)

    # Restore the parent's import path first so module resolution matches.
    for entry in reversed(payload.get("sys_path") or []):
        if entry not in sys.path:
            sys.path.insert(0, entry)

    try:
        fn = resolve_callable(
            payload["kind"], payload.get("code"), payload.get("module"), payload["fn_name"]
        )
        if payload["mode"] == "frame":
            value: Any = fn(*(payload.get("frames") or []))
        elif payload["mode"] == "row":
            value = [fn(rec) for rec in (payload.get("records") or [])]
        else:
            raise ValueError(f"unknown sandbox mode: {payload['mode']!r}")
        out = {"ok": True, "value": value}
    except BaseException as exc:  # noqa: BLE001 — worker boundary: any failure of authored code is reported back to the parent, which re-raises it; nothing is swallowed
        out = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}

    with open(out_path, "wb") as fh:
        pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return 0


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv[1], sys.argv[2]))
