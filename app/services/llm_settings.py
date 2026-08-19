from app.core.llm.options import LLMModel
from app.core.llm.settings import (
    LLMTransformSettings,
    load_llm_transform_settings,
    save_llm_transform_model,
)


def load_global_llm_transform_settings() -> LLMTransformSettings:
    return load_llm_transform_settings()


def list_global_llm_transform_models() -> list[LLMModel]:
    return list(LLMModel)


def save_global_llm_transform_model(value: str) -> LLMTransformSettings:
    model = LLMModel.parse(value, source="llm_transform_model")
    return save_llm_transform_model(model)
