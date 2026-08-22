"""One executor at a time per run: the lease, and the fence over its writes.

docs/run-leases.md
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.core.ids import ID
from app.core.json_types import JsonDict


class RunLease(BaseModel):
    """`fence` rises each time the lease is taken, never on renewal: it names one TENURE."""

    model_config = ConfigDict(frozen=True)

    run_id: ID
    executor_id: str
    fence: int
    expires_at: int


class RunLeaseStore(Protocol):
    """Served by the document store's own handle, because `write_if_held` spans both."""

    def take_lease(self, run_id: ID, executor_id: str, ttl_seconds: int) -> RunLease | None: ...
    def renew_lease(self, lease: RunLease, ttl_seconds: int) -> RunLease | None: ...
    def release_lease(self, lease: RunLease) -> None: ...
    def expire_lease(self, lease: RunLease) -> None: ...
    def read_lease(self, run_id: ID) -> RunLease | None: ...
    def store_now(self) -> int: ...
    def write_if_held(
        self, collection: str, id: ID, data: JsonDict, lease: RunLease, schema_version: int = 1
    ) -> bool: ...
