"""Web-server maintenance that requires the configured stores."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from app.services.run import reconcile_interrupted_runs

RECONCILIATION_INTERVAL_SECONDS = 5.0


@asynccontextmanager
async def maintain_run_reconciliation() -> AsyncIterator[None]:
    reconcile_interrupted_runs()
    task = asyncio.create_task(_reconcile_repeatedly())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _reconcile_repeatedly() -> None:
    while True:
        await asyncio.sleep(RECONCILIATION_INTERVAL_SECONDS)
        reconcile_interrupted_runs()
