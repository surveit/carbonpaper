"""Make `app.*` loggers emit. Called from a composition root, never at import."""
from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from typing import Iterator, TextIO

APP_LOGGER = "app"
_HANDLER_NAME = "carbon_paper-app-console"


def configure_app_logging(
    level: int = logging.INFO, stream: TextIO | None = None
) -> None:
    # Uvicorn leaves root at WARNING with no handler, so app records vanish.
    logger = logging.getLogger(APP_LOGGER)
    # Idempotent on OUR handler, not on any handler: something else attaching one
    # (pytest's capture does) must not silently leave the app unlogged.
    if any(h.get_name() == _HANDLER_NAME for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


@contextmanager
def log_elapsed(logger: logging.Logger, phase: str) -> Iterator[None]:
    # Logs on the way out of a raise too, so a slow phase that then fails still reports.
    start = time.perf_counter()
    try:
        yield
    finally:
        logger.info("%s took %.2fs", phase, time.perf_counter() - start)
