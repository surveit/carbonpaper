"""The persisted, workspace-wide model selection for LLM transforms."""
from __future__ import annotations

from typing import ClassVar

from pydantic import ValidationError

from app.core.llm import DEFAULT_TRANSFORM_MODEL, LLMModel
from app.core.persistence import PersistedModel, PersistenceScope, get_store


class TransformModelSetting(PersistedModel):
    collection: ClassVar[str] = "transform_model_setting"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    model: LLMModel


class TransformModelSettingHistory(PersistedModel):
    collection: ClassVar[str] = "transform_model_setting_history"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ


def initialize_transform_model_setting() -> TransformModelSetting:
    settings = _read_settings()
    history = _read_history()
    if settings:
        setting = _require_one_setting(settings)
        _ensure_history(history)
        return setting
    if history or not get_store().is_empty():
        raise RuntimeError("global LLM-transform model setting is missing")
    setting = TransformModelSetting(model=DEFAULT_TRANSFORM_MODEL)
    setting.save()
    TransformModelSettingHistory().save()
    return setting


def read_transform_model_setting() -> TransformModelSetting:
    return _require_one_setting(_read_settings())


def set_transform_model(model: LLMModel) -> TransformModelSetting:
    setting = read_transform_model_setting()
    setting.model = model
    setting.save()
    return setting


def _read_settings() -> list[TransformModelSetting]:
    try:
        return TransformModelSetting.list()
    except ValidationError as exc:
        raise RuntimeError("global LLM-transform model setting is corrupt") from exc


def _read_history() -> list[TransformModelSettingHistory]:
    try:
        return TransformModelSettingHistory.list()
    except ValidationError as exc:
        raise RuntimeError("global LLM-transform model setting history is corrupt") from exc


def _ensure_history(history: list[TransformModelSettingHistory]) -> None:
    if not history:
        TransformModelSettingHistory().save()
        return
    if len(history) != 1:
        raise RuntimeError("global LLM-transform model setting history is not unique")


def _require_one_setting(settings: list[TransformModelSetting]) -> TransformModelSetting:
    if not settings:
        raise RuntimeError("global LLM-transform model setting is missing")
    if len(settings) != 1:
        raise RuntimeError("global LLM-transform model setting is not unique")
    return settings[0]
