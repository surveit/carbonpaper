from app.core.llm.options import LLMModel
from app.core.llm.settings import (
    LLMTransformSettings,
    load_llm_transform_settings,
    save_llm_transform_model,
)


def test_missing_llm_transform_settings_are_materialized_once():
    settings = load_llm_transform_settings()

    assert settings.selected_model == LLMModel.claude_haiku_4_5
    assert settings.id not in {"global", "llm_transform"}
    assert LLMTransformSettings.list() == [settings]


def test_global_llm_transform_model_round_trips_codex_choice():
    saved = save_llm_transform_model(LLMModel.gpt_5_6_terra)

    loaded = load_llm_transform_settings()

    assert loaded.id == saved.id
    assert loaded.selected_model == LLMModel.gpt_5_6_terra
    model = LLMModel.parse(loaded.selected_model, source="test")
    assert model.backend == "codex"
