"""Daemon-thread launch, in one place: the only sanctioned `except Exception` on a
thread, so a crash off the request path is never swallowed silently."""
from __future__ import annotations

import threading
import traceback
from collections.abc import Callable


def run_in_background(
    work: Callable[[], object],
    *,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """`on_error` gets the formatted traceback; unset, it prints. `work`'s return is dropped."""
    def _guarded() -> None:
        try:
            work()
        except Exception:  # noqa: BLE001 — a daemon thread's crash has nowhere else to surface
            if on_error is None:
                traceback.print_exc()
            else:
                on_error(traceback.format_exc())

    threading.Thread(target=_guarded, daemon=True).start()
