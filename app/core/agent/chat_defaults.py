"""The persisted backend selected for newly opened chats."""
from __future__ import annotations

from typing import ClassVar

from app.core.agent.store import ChatBackend
from app.core.persistence import PersistedModel, PersistenceScope


class ChatDefaultSetting(PersistedModel):
    collection: ClassVar[str] = "chat_default"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    backend: ChatBackend


class ChatDefaultInitialization(PersistedModel):
    collection: ClassVar[str] = "chat_default_initialization"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ


def read_default_chat_backend() -> ChatBackend:
    initialize_default_chat_backend()
    [setting] = _read_single_setting()
    return setting.backend


def set_default_chat_backend(backend: ChatBackend) -> None:
    initialize_default_chat_backend()
    [setting] = _read_single_setting()
    setting.backend = backend
    setting.save()


def initialize_default_chat_backend() -> None:
    initializations = ChatDefaultInitialization.list()
    if len(initializations) > 1:
        raise RuntimeError("chat default initialization has multiple records")
    if initializations:
        return
    if ChatDefaultSetting.list():
        raise RuntimeError("chat default setting exists without initialization")
    ChatDefaultSetting(backend=ChatBackend.claude).save()
    ChatDefaultInitialization().save()


def _read_single_setting() -> list[ChatDefaultSetting]:
    settings = ChatDefaultSetting.list()
    if not settings:
        raise RuntimeError("chat default setting is missing after initialization")
    if len(settings) > 1:
        raise RuntimeError("chat default setting has multiple records")
    return settings
