from __future__ import annotations

import pytest

from app.core.llm import LLMModel
from app.core.transform_model_settings import (
    TransformModelSetting,
    initialize_transform_model_setting,
    read_transform_model_setting,
    set_transform_model,
)


def test_initialize_materializes_the_existing_claude_model_once() -> None:
    initialize_transform_model_setting()

    setting = read_transform_model_setting()

    assert setting.model == LLMModel.claude_haiku_4_5
    assert len(TransformModelSetting.list()) == 1


def test_runtime_refuses_an_uninitialized_global_setting() -> None:
    from app.core.persistence import configure_store
    from app.core.sqlite_store import SqliteKvStore

    configure_store(SqliteKvStore(":memory:"))

    with pytest.raises(RuntimeError, match="global LLM-transform model setting is missing"):
        read_transform_model_setting()


def test_setting_the_global_model_persists_the_selected_model() -> None:
    initialize_transform_model_setting()

    set_transform_model(LLMModel.gpt_5_6_terra)

    assert read_transform_model_setting().model == LLMModel.gpt_5_6_terra


def test_initialization_refuses_a_setting_deleted_after_selection() -> None:
    initialize_transform_model_setting()
    selected = set_transform_model(LLMModel.gpt_5_6_terra)
    TransformModelSetting.delete(selected.id)

    with pytest.raises(RuntimeError, match="global LLM-transform model setting is missing"):
        initialize_transform_model_setting()


def test_initialization_refuses_to_materialize_a_setting_in_an_existing_database() -> None:
    from app.core.persistence import configure_store, get_store
    from app.core.sqlite_store import SqliteKvStore

    configure_store(SqliteKvStore(":memory:"))
    get_store().write("unrelated", "record", {})

    with pytest.raises(RuntimeError, match="global LLM-transform model setting is missing"):
        initialize_transform_model_setting()


def test_initialization_refuses_a_corrupt_setting() -> None:
    from app.core.persistence import get_store

    selected = read_transform_model_setting()
    get_store().write(
        TransformModelSetting.collection,
        selected.id,
        {**selected.model_dump(mode="json"), "model": "not-a-model"},
    )

    with pytest.raises(RuntimeError, match="global LLM-transform model setting is corrupt"):
        initialize_transform_model_setting()
