from __future__ import annotations

from app.core.llm.options import LLMModel
from app.core.persistence import PersistenceScope, PersistedModel

DEFAULT_LLM_TRANSFORM_MODEL = LLMModel.claude_haiku_4_5


class LLMTransformSettings(PersistedModel):
    collection = "llm_transform_settings"
    SCOPE = PersistenceScope.PROJECT_READ_WRITE

    selected_model: LLMModel = DEFAULT_LLM_TRANSFORM_MODEL


def load_llm_transform_settings() -> LLMTransformSettings:
    records = LLMTransformSettings.list()
    if len(records) > 1:
        raise RuntimeError(
            f"expected one llm_transform settings record, found {len(records)}"
        )
    if records:
        return records[0]
    settings = LLMTransformSettings()
    settings.save()
    return settings


def save_llm_transform_model(model: LLMModel) -> LLMTransformSettings:
    settings = load_llm_transform_settings()
    settings.selected_model = model
    settings.save()
    return settings
