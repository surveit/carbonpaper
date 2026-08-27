from __future__ import annotations

from app.models import parse_stage


def _row_function_stage(input_id="src", **overrides):
    base = {
        "id": "step",
        "type": "python_row_function",
        "description": "Step",
        "inputs": [{"id": input_id}],
        "signature": {"form": "extends"},
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    }
    base.update(overrides)
    return parse_stage(base)


def _queue_stage(**queue_overrides):
    queue = {
        "filter": "score > 0.5",
        "reviewer_instructions": "check it",
        "routing": "team-a",
        "conflict_resolution": "escalate",
        "estimated_volume_per_week": 10,
        "reviewed_columns": {"score": "human_score"},
        "verdict_column": "decision",
        "reviewer_column": "reviewer_id",
        "reviewed_at_column": "reviewed_at",
    }
    queue.update(queue_overrides)
    return parse_stage({
        "id": "review",
        "type": "human_review_queue",
        "description": "review",
        "inputs": [{"id": "src"}],
        # Every column the queue adds, since the signature must declare them all,
        # over the whole anchor edge, since a queue reads the row it shows.
        "signature": {"form": "extends", "reads": [{"input": "src", "columns": [
            {"name": "id", "type": "str", "nullable": False},
            {"name": "score", "type": "float", "nullable": False},
        ]}], "adds": [
            {"name": "human_score", "type": "float", "nullable": True},
            {"name": "decision", "type": "str", "nullable": True},
            {"name": "reviewer_id", "type": "str", "nullable": True},
            {"name": "reviewed_at", "type": "str", "nullable": True},
        ]},
        "queue": queue,
    })


def _report_stage(code="def transform(df, output_dir, citation_provider):\n    return df\n", **overrides):
    base = {
        "id": "report",
        "type": "report",
        "description": "report",
        "inputs": [{"id": "src"}],
        "report": {"format": "html_report", "destination": "out/"},
        "function": {"kind": "inline", "code": code},
        "signature": {"form": "replaces"},
    }
    base.update(overrides)
    return parse_stage(base)


def test_compute_definition_fingerprint_is_deterministic():
    stage = _row_function_stage()
    assert stage.compute_definition_fingerprint() == stage.compute_definition_fingerprint()


def test_compute_definition_fingerprint_ignores_incidental_fields():
    a = _row_function_stage(input_id="src_a", id="step_a", description="Step A")
    b = _row_function_stage(input_id="src_b", id="step_b", description="Step B")
    assert a.compute_definition_fingerprint() == b.compute_definition_fingerprint()


def test_compute_definition_fingerprint_changes_with_config_block_content():
    a = _row_function_stage()
    b = _row_function_stage(
        function={"kind": "inline", "code": "def transform(row):\n    row['x'] = 1\n    return row\n"}
    )
    assert a.compute_definition_fingerprint() != b.compute_definition_fingerprint()


def test_compute_definition_fingerprint_changes_with_the_signature():
    a = _row_function_stage()
    b = _row_function_stage(
        signature={"form": "extends",
                   "adds": [{"name": "b", "type": "str", "nullable": True}]}
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


def test_compute_definition_fingerprint_for_queue_reacts_to_context_columns():
    base = _queue_stage()
    changed = _queue_stage(context_columns=["id"])

    assert base.compute_definition_fingerprint() != changed.compute_definition_fingerprint()


def test_compute_definition_fingerprint_for_report_reacts_to_function_code():
    base = _report_stage()
    changed = _report_stage(
        code="def transform(df, output_dir, citation_provider):\n    return df.head(1)\n"
    )
    assert base.compute_definition_fingerprint() != changed.compute_definition_fingerprint()


def test_compute_definition_fingerprint_for_report_reacts_to_report_block():
    base = _report_stage()
    changed = _report_stage(report={"format": "html_report", "destination": "elsewhere/"})
    assert base.compute_definition_fingerprint() != changed.compute_definition_fingerprint()


def test_compute_definition_fingerprint_survives_a_stored_round_trip():
    stage = _queue_stage()
    # app.models.stage_to_spec_dict's exact dump options — the shape a WorkflowVersion stores.
    dumped = stage.model_dump(mode="json", by_alias=True, exclude_none=True)
    reloaded = parse_stage(dumped)
    assert stage.compute_definition_fingerprint() == reloaded.compute_definition_fingerprint()
