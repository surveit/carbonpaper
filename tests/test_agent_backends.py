"""build_engine builds the engine an agent's backend names, with that backend's settings."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import BaseModel, ValidationError

from app.core.agent import registry
from app.core.agent.api_engine import AnthropicApiEngine
from app.core.agent.bound_tool import BoundToolSpec, bind_by_signature
from app.core.agent.engine import ChatEngine
from app.core.agent.registry import AgentConfig, ApiBackend, CliBackend, build_engine, register
from app.core.agent.sdk_engine import MCP_SERVER_NAME, ClaudeAgentSdkEngine

_HAIKU = "claude-haiku-5-5"


class _Ctx(BaseModel):
    label: str = "anything"


def _build_echo_tool(context: BaseModel) -> list[BoundToolSpec]:
    def echo() -> str:
        return "echo"

    return [bind_by_signature(
        name="echo", description="Echo.", fn=echo, label="Echoing", parameters={},
    )]


@pytest.fixture(autouse=True)
def forget_the_probe() -> Iterator[None]:
    yield
    registry._registry.pop("probe", None)


def _build_probe(backend: CliBackend | ApiBackend) -> ChatEngine:
    config = AgentConfig(
        system_prompt="sp", context_schema=_Ctx, display_name="Probe", backend=backend,
    )
    register("probe", config, _build_echo_tool)
    return build_engine("probe", {})


def test_an_api_backend_builds_the_api_engine_on_its_model_and_effort() -> None:
    engine = _build_probe(ApiBackend(model=_HAIKU, effort="medium"))

    assert isinstance(engine, AnthropicApiEngine)
    assert (engine._model, engine._effort) == (_HAIKU, "medium")
    assert list(engine._tools) == ["echo"]
    assert engine._tool_labels == {"echo": "Echoing"}


def test_a_cli_backend_builds_the_cli_engine_on_its_model_and_thinking() -> None:
    engine = _build_probe(CliBackend(model="opus", thinking={"type": "disabled"}))

    assert isinstance(engine, ClaudeAgentSdkEngine)
    assert (engine._model, engine._thinking) == ("opus", {"type": "disabled"})
    assert engine._allowed_tools == [f"mcp__{MCP_SERVER_NAME}__echo"]
    assert engine._tool_labels == {"echo": "Echoing"}


def test_an_api_backend_on_an_unpriced_model_is_refused_where_it_is_declared() -> None:
    with pytest.raises(ValidationError, match="claude-opus-5 has no price"):
        ApiBackend(model="claude-opus-5", effort="low")
