"""Compiler authoring-prompt content: llm_transform's prompt/data split."""
from app.compiler import workflow_prompt
from app.compiler.node_contract_notes import HUMAN_REVIEW_QUEUE_CONTRACT_NOTE
from app.compiler.workflow_prompt import WORKFLOW_SYSTEM_PROMPT
from app import models


def test_workflow_prompt_teaches_split() -> None:
    text = "\n".join(
        v for v in vars(workflow_prompt).values() if isinstance(v, str)
    )
    assert "prompt_instructions" in text
    assert "prompt_data_template" in text
    assert "cacheable" in text or "cache" in text


def test_prompt_has_shared_blocks() -> None:
    assert "METHODOLOGY COMPILER" in WORKFLOW_SYSTEM_PROMPT
    assert "NEVER fabricate data values" in WORKFLOW_SYSTEM_PROMPT
    assert "Optimize for reviewability" in WORKFLOW_SYSTEM_PROMPT


def test_workflow_prompt_teaches_llm_split() -> None:
    assert "prompt_instructions" in WORKFLOW_SYSTEM_PROMPT
    assert "prompt_data_template" in WORKFLOW_SYSTEM_PROMPT
    assert "cache" in WORKFLOW_SYSTEM_PROMPT


def test_workflow_prompt_catalogue_covers_node_types() -> None:
    for name in models.NODE_TYPES:
        assert f"- {name} —" in WORKFLOW_SYSTEM_PROMPT


def test_hrq_line_has_both_notes() -> None:
    lines = WORKFLOW_SYSTEM_PROMPT.splitlines()
    hrq_line = next(line for line in lines if line.startswith("- human_review_queue —"))
    # Both notes must reach the one catalogue line: the row-identity/matching note
    # (rows are matched to a cached decision by fingerprinting the row) AND the
    # output-columns contract note.
    assert "fingerprint" in hrq_line
    assert "decision" in hrq_line or "output columns are FIXED" in hrq_line


def test_hrq_note_names_the_decision_values_the_runtime_actually_emits() -> None:
    """The note tells an author to exclude rejected rows with
    `decision != "reject"`. That instruction is only correct while the strings
    it names are the ones the queue handler writes — including the value it puts
    on a row the queue filter passed through unreviewed, which is what makes the
    documented filter safe without reasoning about a missing value. Pinned
    against the handler's own constants so the guidance cannot drift from the
    runtime it describes."""
    from app.models import RowReviewDecision
    from app.runtime.stages.human_review_queue import NOT_REVIEWED

    for value in (RowReviewDecision.reject.value, RowReviewDecision.approve.value,
                  RowReviewDecision.modify.value, NOT_REVIEWED):
        assert f'"{value}"' in HUMAN_REVIEW_QUEUE_CONTRACT_NOTE, value
