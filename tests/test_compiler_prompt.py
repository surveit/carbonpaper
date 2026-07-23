"""Compiler authoring-prompt content: llm_transform's prompt/data split."""
from app.compiler import prompt as compiler_prompt
from app.compiler import workflow_prompt


def test_workflow_prompt_teaches_split() -> None:
    text = "\n".join(
        v for v in vars(workflow_prompt).values() if isinstance(v, str)
    )
    assert "prompt_instructions" in text
    assert "prompt_data_template" in text
    assert "cacheable" in text or "cache" in text


def test_compiler_fewshot_uses_split() -> None:
    text = repr(compiler_prompt._EXAMPLE_STAGE) + "\n" + compiler_prompt.build_compile_prompt(
        "irrelevant input", "example"
    )
    assert "prompt_instructions" in text
    assert "prompt_data_template" in text
    assert '"prompt_template"' not in text
    assert "'prompt_template'" not in text
