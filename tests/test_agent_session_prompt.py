"""AgentConfig.render_session_prompt — prose only one session's context can supply,
appended to the static system prompt. The generic half knows nothing about terms; the
editing agent's half is what makes an agent write in its project's words."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import BaseModel, ValidationError

from app.agents.compiler.config import CONFIG as EDITING_CONFIG, _render_project_binding
from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
from app.core.agent import registry
from app.core.agent.registry import AgentConfig, build_engine, register, render_system_prompt
from app.models import NamedSchema, SchemaLibrary, Terms, Verb
from app.services import terms as terms_service
from app.services import project as project_service
from app.services import workspace
from app.tools.editing import EditingContext
from app.tools.prompt_fragments import render_link_map

_FILING = NamedSchema(
    name="filing",
    title="Filing",
    description="One disclosure a firm sent in.",
    also_written=["disclosure"],
)
_FLAG = Verb(name="flag", definition="Mark a row for a human to decide on.")


class _Ctx(BaseModel):
    label: str


def _no_tools(ctx: BaseModel) -> list:
    return []


def _render_label(ctx: BaseModel) -> str:
    assert isinstance(ctx, _Ctx)
    return f"words for {ctx.label}"


@pytest.fixture
def dummy_agent() -> Iterator[None]:
    yield
    registry._registry.pop("dummy", None)


# ── the generic hook ─────────────────────────────────────────────────────────
def test_an_agent_with_no_hook_is_handed_its_static_prompt_unchanged() -> None:
    config = AgentConfig(system_prompt="sp", context_schema=_Ctx, display_name="Bare")
    assert render_system_prompt(config, _Ctx(label="anything")) == "sp"


def test_the_hook_reads_the_validated_context_and_its_answer_is_appended() -> None:
    config = AgentConfig(
        system_prompt="sp",
        context_schema=_Ctx,
        display_name="Bare",
        render_session_prompt=_render_label,
    )
    assert render_system_prompt(config, _Ctx(label="ONE")) == "sp\n\nwords for ONE"


def test_a_hook_with_nothing_to_say_appends_nothing_at_all() -> None:
    # Not even the separator: a blank tail reads as a section the agent was denied.
    config = AgentConfig(
        system_prompt="sp", context_schema=_Ctx, display_name="Bare",
        render_session_prompt=lambda ctx: ""
    )
    assert render_system_prompt(config, _Ctx(label="anything")) == "sp"


def test_build_engine_hands_the_appended_prompt_to_the_engine(dummy_agent) -> None:
    register(
        "dummy",
        AgentConfig(
            system_prompt="sp",
            context_schema=_Ctx,
            display_name="Bare",
            render_session_prompt=_render_label,
        ),
        _no_tools,
    )
    engine = build_engine("dummy", {"label": "alpha"})
    assert engine._system_prompt == "sp\n\nwords for alpha"


# ── the editing agent's use of it ────────────────────────────────────────────
def _project_with(tmp_path, terms: Terms | None) -> str:
    workspace.set_projects_dir(tmp_path)
    project_id = project_service.create_project("vocab", "doc text", source="test").id
    if terms is not None:
        terms_service.write_terms(project_id, terms)
    return project_id


_READER = {"base_url": "https://carbon.example/"}


def test_the_editing_agent_is_handed_its_projects_words(tmp_path) -> None:
    project_id = _project_with(
        tmp_path, Terms(nouns=SchemaLibrary(schemas=[_FILING]), verbs=[_FLAG])
    )

    prompt = build_engine("editing", {"project_id": project_id} | _READER)._system_prompt

    assert prompt.startswith(EDITING_SYSTEM_PROMPT)
    assert "- filing — One disclosure a firm sent in. Also written: disclosure." in prompt
    assert "- flag — Mark a row for a human to decide on." in prompt


def test_a_project_that_has_agreed_no_words_appends_no_words(tmp_path) -> None:
    project_id = _project_with(tmp_path, None)
    context = EditingContext(project_id=project_id, **_READER)

    prompt = build_engine("editing", {"project_id": project_id} | _READER)._system_prompt

    assert prompt == "\n\n".join([
        EDITING_SYSTEM_PROMPT,
        _render_project_binding(context),
        render_link_map(_READER["base_url"]),
    ])


def test_the_editing_agent_is_handed_the_pages_it_can_link_to(tmp_path) -> None:
    project_id = _project_with(tmp_path, None)

    prompt = build_engine("editing", {"project_id": project_id} | _READER)._system_prompt

    assert "https://carbon.example/project/<project_id>/workflow" in prompt
    assert "https://carbon.example/project/<project_id>/runs/<run_id>" in prompt


def test_a_session_with_no_address_is_refused(tmp_path) -> None:
    # Every caller has one: a route off the request, the dump off a placeholder host.
    workspace.set_projects_dir(tmp_path)

    with pytest.raises(ValidationError):
        build_engine("editing", {"project_id": _project_with(tmp_path, None)})


def test_the_words_and_the_links_both_reach_one_session(tmp_path) -> None:
    project_id = _project_with(
        tmp_path, Terms(nouns=SchemaLibrary(schemas=[_FILING]), verbs=[_FLAG])
    )

    prompt = build_engine("editing", {"project_id": project_id} | _READER)._system_prompt

    assert "https://carbon.example/project/<project_id>/workflow" in prompt
    assert "- flag — Mark a row for a human to decide on." in prompt


def test_a_session_bound_to_no_project_is_still_told_where_its_reader_is(tmp_path) -> None:
    workspace.set_projects_dir(tmp_path)

    prompt = build_engine("editing", dict(_READER))._system_prompt

    assert prompt == "\n\n".join([
        EDITING_SYSTEM_PROMPT,
        _render_project_binding(EditingContext(**_READER)),
        render_link_map(_READER["base_url"]),
    ])


def test_the_hook_the_editing_agent_registered_is_the_one_that_runs() -> None:
    # Without this, the two tests above would pass on an agent carrying no hook.
    assert EDITING_CONFIG.render_session_prompt is not None
