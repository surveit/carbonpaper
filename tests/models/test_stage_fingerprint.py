from __future__ import annotations

from app.models import Stage


def _row_function_stage(input_id="src", **overrides):
    base = {
        "id": "step",
        "type": "python_row_function",
        "name": "Step",
        "inputs": [{
            "id": input_id,
            "schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]},
        }],
        "output_schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]},
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    }
    base.update(overrides)
    return Stage.model_validate(base)


def _queue_stage(**queue_overrides):
    queue = {
        "filter": "score > 0.5",
        "reviewer_instructions": "check it",
        "routing": "team-a",
        "conflict_resolution": "escalate",
        "estimated_volume_per_week": 10,
    }
    queue.update(queue_overrides)
    return Stage.model_validate({
        "id": "review",
        "type": "human_review_queue",
        "name": "review",
        "inputs": [{
            "id": "src",
            "schema": {"columns": [
                {"name": "id", "type": "str", "nullable": False},
                {"name": "score", "type": "float", "nullable": False},
            ]},
        }],
        "output_schema": {"columns": [{"name": "id", "type": "str", "nullable": False}]},
        "queue": queue,
    })


def test_compute_definition_fingerprint_is_deterministic():
    stage = _row_function_stage()
    assert stage.compute_definition_fingerprint() == stage.compute_definition_fingerprint()


def test_compute_definition_fingerprint_ignores_incidental_fields():
    a = _row_function_stage(input_id="src_a", id="step_a", name="Step A")
    b = _row_function_stage(input_id="src_b", id="step_b", name="Step B")
    assert a.compute_definition_fingerprint() == b.compute_definition_fingerprint()


def test_compute_definition_fingerprint_changes_with_handle_content():
    a = _row_function_stage()
    b = _row_function_stage(
        function={"kind": "inline", "code": "def transform(row):\n    row['x'] = 1\n    return row\n"}
    )
    assert a.compute_definition_fingerprint() != b.compute_definition_fingerprint()


def test_compute_definition_fingerprint_changes_with_output_schema():
    a = _row_function_stage()
    b = _row_function_stage(
        output_schema={"columns": [
            {"name": "a", "type": "str", "nullable": False},
            {"name": "b", "type": "str", "nullable": True},
        ]}
    )
    assert a.compute_definition_fingerprint() != b.compute_definition_fingerprint()


def test_compute_definition_fingerprint_for_queue_ignores_routing_metadata():
    base = _queue_stage()
    changed = _queue_stage(
        routing="team-b", conflict_resolution="auto", estimated_volume_per_week=999,
    )
    assert base.compute_definition_fingerprint() == changed.compute_definition_fingerprint()


def test_compute_definition_fingerprint_for_queue_reacts_to_filter():
    base = _queue_stage()
    changed = _queue_stage(filter="score > 0.9")
    assert base.compute_definition_fingerprint() != changed.compute_definition_fingerprint()


def test_compute_definition_fingerprint_for_queue_reacts_to_reviewer_instructions():
    base = _queue_stage()
    changed = _queue_stage(reviewer_instructions="check twice")
    assert base.compute_definition_fingerprint() != changed.compute_definition_fingerprint()


def _sql_stage(query: str = "SELECT * FROM src"):
    return Stage.model_validate({
        "id": "q", "type": "sql_transform", "name": "q",
        "inputs": [{
            "id": "src",
            "schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]},
        }],
        "output_schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]},
        "sql": {"query": query},
    })


def test_compute_definition_fingerprint_changes_with_sql_query_text():
    a = _sql_stage()
    b = _sql_stage(query="SELECT * FROM src WHERE a IS NOT NULL")
    assert a.compute_definition_fingerprint() != b.compute_definition_fingerprint()


def test_compute_definition_fingerprint_survives_a_stored_round_trip():
    # A version-embedded stage is dumped/reloaded through
    # model_dump(mode="json", by_alias=True, exclude_none=True) —
    # app.services.loader.stage_to_spec_dict's exact dump options, the shape
    # a WorkflowVersion stores. Round-tripping a queue stage through that dump
    # (a rich handle block, QueueConfig, plus output_schema's nested Column
    # list) must reproduce the same fingerprint.
    stage = _queue_stage()
    dumped = stage.model_dump(mode="json", by_alias=True, exclude_none=True)
    reloaded = Stage.model_validate(dumped)
    assert stage.compute_definition_fingerprint() == reloaded.compute_definition_fingerprint()
