"""Architecture: the prose at a function's, method's, or class's ENTRANCE in ``app/`` and
``tests/`` — its docstring PLUS the comment block above the first statement — at or under 100
characters TOGETHER. One budget over both syntaxes, because prose above the first statement
costs the reader what a docstring costs. `_GRANDFATHERED` / `_GRANDFATHERED_ENTRANCE_PROSE`
(may only shrink) / `_JUSTIFIED_EXCEPTIONS` (rare); else cut it, or move it to docs/.
"""
from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass
from pathlib import Path

import pytest

from arch.test_complexity_ratchet import find_overload_stub_lines
from arch.test_module_docstring_ratchet import find_governed_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_TESTS_ROOT = _REPO_ROOT / "tests"
_PROSE_CHAR_CEILING = 100

_DEFINITION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# Symbols already over the ceiling on their docstring alone when this rule landed. No
# per-entry reason: the reason is uniform — they predate the rule. A ratchet, so entries
# may only be REMOVED, never added; a new offender cuts its prose instead. Keyed on the
# SYMBOL (`path::Qualified.name`), never a line number, which would rot on the next
# unrelated edit. An entry whose symbol is gone, or whose entrance prose is now at or
# under the ceiling, fails loud as stale — that is what makes this a burn-down list
# rather than a parking lot.
_GRANDFATHERED: frozenset[str] = frozenset(
    {
        "app/_arch_tests/test_persisted_models_declare_scope.py::_assigns_project_read_write",
        "app/_arch_tests/test_persisted_models_declare_scope.py::find_project_read_write_missing_read_only_offenders",
        "app/_arch_tests/test_persisted_models_declare_scope.py::find_undeclared_scope_offenders",
        "app/_arch_tests/test_persisted_models_declare_scope.py::test_find_project_read_write_missing_read_only_offenders_ignores_a_class_missing_scope",
        "app/_arch_tests/test_persisted_models_declare_scope.py::test_find_undeclared_scope_offenders_rejects_a_bare_annotation_with_no_value",
        "app/compiler/data_model.py::_frame",
        "app/compiler/data_model.py::build_data_model_agent",
        "app/compiler/data_model.py::start_data_model_generation_agent",
        "app/compiler/stage_tests.py::_render_corner_cases",
        "app/compiler/stage_tests.py::build_stage_test_generator",
        "app/compiler/stage_tests.py::render_generation_task",
        "app/compiler/stage_tests.py::start_stage_test_generation_agent",
        "app/core/agent/agent.py::Agent",
        "app/core/agent/agent.py::Agent.answer",
        "app/core/agent/agent.py::Agent.build_engine",
        "app/core/agent/agent.py::Agent.last_usage",
        "app/core/agent/agent.py::Agent.run",
        "app/core/agent/agent.py::Agent.task",
        "app/core/agent/diagnostics.py::AgentRunDiagnostics._render_availability",
        "app/core/agent/diagnostics.py::_read_init_inventories",
        "app/core/agent/diagnostics.py::_read_tool_advertised",
        "app/core/agent/registry.py::build_engine",
        "app/core/agent/registry.py::register",
        "app/core/agent/sdk_engine.py::ClaudeAgentSdkEngine",
        "app/core/agent/sdk_engine.py::_stringify",
        "app/core/agent/sdk_engine.py::_usage_from_result",
        "app/core/agent/session.py::create_agent_session",
        "app/core/agent/store.py::AgentSession",
        "app/core/agent/store.py::SessionStore",
        "app/core/agent/store.py::SessionStore.create",
        "app/core/agent/store.py::SessionStore.load_messages",
        "app/core/agent/store.py::SessionStore.resume_token",
        "app/core/agent/store.py::_render_history_bubbles",
        "app/core/agent/store.py::open_session_store",
        "app/core/agent/turns.py::default_turn_manager",
        "app/core/agent/usage.py::LlmUsage",
        "app/core/errors.py::DocumentNotFound",
        "app/core/errors.py::DraftNotFoundError",
        "app/core/errors.py::EvalGrainViolationError",
        "app/core/errors.py::EvalNotScorableError",
        "app/core/errors.py::FrameNotSerializableError",
        "app/core/errors.py::GenerationError",
        "app/core/errors.py::MissingInputBindingError",
        "app/core/errors.py::NoVersionToRunError",
        "app/core/errors.py::NoWorkflowTestSourceError",
        "app/core/errors.py::NoWorkflowTestVersionError",
        "app/core/errors.py::PredicateError",
        "app/core/errors.py::ProjectExistsError",
        "app/core/errors.py::RowOutOfRange",
        "app/core/errors.py::RunNotFoundError",
        "app/core/errors.py::RunVersionUnresolvableError",
        "app/core/errors.py::StageNotInRun",
        "app/core/errors.py::SubsetRunError",
        "app/core/errors.py::TraceLinksUnavailableError",
        "app/core/frames.py::collapse_null_forms",
        "app/core/frames.py::compute_frame_fingerprint",
        "app/core/frames.py::compute_frames_fingerprint",
        "app/core/frames.py::configure_frame_store",
        "app/core/frames.py::convert_cell_to_json_native",
        "app/core/frames.py::list_rows",
        "app/core/frames.py::save_frame_or_reject",
        "app/core/llm_sdk.py::find_cli",
        "app/core/llm_sdk.py::run_sync",
        "app/core/paths.py::repo_root",
        "app/core/persistence.py::PersistedModel",
        "app/core/persistence.py::PersistenceScope",
        "app/core/persistence.py::SqliteKvStore",
        "app/core/persistence.py::SqliteKvStore._scan",
        "app/core/persistence.py::configure_store",
        "app/core/persistence.py::validate_id",
        "app/core/predicate.py::ParsedPredicate",
        "app/core/predicate.py::_normalize",
        "app/core/predicate.py::_validate_node",
        "app/core/predicate.py::parse_predicate",
        "app/core/prompt_template.py::find_template_fields",
        "app/core/run_status.py::RunStatus",
        "app/core/stage_cache.py::ReadOnlyStageCache",
        "app/core/stage_cache.py::ReadOnlyStageCache.find_cached_frame",
        "app/core/stage_cache.py::ReadOnlyStageCache.find_recorded_rows",
        "app/core/stage_cache.py::StageCache.record",
        "app/core/stage_cache.py::StageCache.record_frame",
        "app/core/stage_cache.py::StageCacheEntry",
        "app/core/stage_cache.py::_build_cache_id",
        "app/core/stage_cache.py::_build_frame_cache_id",
        "app/core/stage_cache.py::_to_json_safe_row",
        "app/core/stage_cache.py::compute_row_fingerprint",
        "app/core/store_config.py::_configure_default_frame_store",
        "app/core/store_config.py::configure_default_stores",
        "app/core/utils.py::compute_short_hash",
        "app/core/utils.py::generate_word_triplet_id",
        "app/evals/compatibility.py::_find_descendants",
        "app/evals/compatibility.py::_resolve_grain_settings",
        "app/evals/compatibility.py::_validate_columns_covered",
        "app/evals/compatibility.py::_validate_eval_dataset_covers_override",
        "app/evals/compatibility.py::_validate_no_reference_override_on_target",
        "app/evals/compatibility.py::_validate_override_declares_output_schema",
        "app/evals/compatibility.py::_validate_reference_overrides_cover_stages",
        "app/evals/compatibility.py::_validate_stages_exist",
        "app/evals/compatibility.py::_validate_target_emits_checked_columns",
        "app/evals/compatibility.py::_validate_target_reachable",
        "app/evals/compatibility.py::validate_eval_compatibility",
        "app/evals/dataset_columns.py::_deconflicted_columns",
        "app/evals/dataset_columns.py::deconflict_column_names",
        "app/evals/dataset_columns.py::get_injected_columns",
        "app/evals/dataset_columns.py::get_output_columns_from_stage",
        "app/evals/run_settings.py::resolve_eval_run_settings",
        "app/evals/runner.py::_build_injected_outputs",
        "app/evals/runner.py::_compute_override_output",
        "app/evals/runner.py::_read_table_ref",
        "app/evals/runner.py::_require_runnable",
        "app/evals/runner.py::_resolve_version",
        "app/evals/runner.py::_score_run",
        "app/evals/runner.py::run_eval",
        "app/evals/scoring.py::ScoreResult",
        "app/evals/scoring.py::_Check",
        "app/evals/scoring.py::_build_per_row_results",
        "app/evals/scoring.py::_resolve_checks",
        "app/evals/scoring.py::_roll_up_metrics",
        "app/evals/scoring.py::_value_matches",
        "app/evals/scoring.py::score_expected_outputs",
        "app/evals/store.py::EvalConfigEntry",
        "app/evals/store.py::eval_status",
        "app/evals/store.py::latest_version_id",
        "app/evals/store.py::list_eval_configs",
        "app/evals/store.py::list_eval_runs",
        "app/evals/store.py::load_eval_config",
        "app/evals/store.py::load_eval_run",
        "app/evals/store.py::save_dataset_upload",
        "app/evals/store.py::save_eval_config",
        "app/evals/store.py::save_eval_run",
        "app/mcp/server.py::_StreamableHTTPEndpoint",
        "app/mcp/server.py::_read_document",
        "app/mcp/server.py::_resolve_existing_project",
        "app/mcp/server.py::catch_stage_edit_refusals",
        "app/mcp/server.py::run_session_manager",
        "app/models/coverage.py::Coverage",
        "app/models/eval.py::CodeScorer",
        "app/models/eval.py::EvalConfig",
        "app/models/eval.py::EvalRunSettings",
        "app/models/eval.py::ExpectedOutput",
        "app/models/eval.py::ScoringMetric",
        "app/models/eval.py::StageOutputOverride",
        "app/models/eval.py::_validate_slug",
        "app/models/named_schemas.py::NamedColumn",
        "app/models/named_schemas.py::NamedSchema",
        "app/models/named_schemas.py::validate_named_schema",
        "app/models/schema.py::Column._range_is_numeric_bounds",
        "app/models/schema.py::StageConfig",
        "app/models/schema.py::TableSchema",
        "app/models/schema.py::TableSchema.differing_column_names",
        "app/models/schema.py::TableSchema.find_unsatisfied_columns",
        "app/models/schema.py::TableSchema.is_subset_of",
        "app/models/schema.py::TableSchema.subtract",
        "app/models/schema.py::TableSchema.to_prompt",
        "app/models/schema.py::TableSchema.to_pydantic_model",
        "app/models/schema.py::_Base",
        "app/models/schema.py::_column_spec_differences",
        "app/models/schema.py::_fields_spec_equal",
        "app/models/schema.py::_is_range_bound",
        "app/models/schema.py::_numeric_range",
        "app/models/schema.py::_render_column",
        "app/models/schema.py::_type_wording",
        "app/models/stage.py::StageDraft",
        "app/models/stage.py::StageDraft._drop_server_owned_fields",
        "app/models/stage.py::StageDraft.to_stage_spec",
        "app/models/stage.py::parse_stage",
        "app/models/stages/aggregate.py::compute_aggregate_output_types",
        "app/models/stages/aggregate.py::find_aggregate_column_issues",
            "app/models/stages/code.py::AuthoredCode",
        "app/models/stages/code.py::PythonFunction",
        "app/models/stages/code.py::PythonFunction._inline_code_is_runnable",
        "app/models/stages/code.py::_binds_name",
        "app/models/stages/code.py::validate_inline_function_code",
        "app/models/stages/filter_rows.py::FilterConfig",
        "app/models/stages/join.py::JoinStage",
        "app/models/stages/llm_transform.py::LLMTransformStage._one_to_one",
        "app/models/stages/llm_transform.py::LLMTransformStage.llm_reply_schema",
        "app/models/stages/llm_transform.py::find_double_braced_input_issues",
        "app/models/stages/llm_transform.py::find_llm_one_to_one_issues",
        "app/models/stages/llm_transform.py::find_llm_prompt_column_issues",
        "app/models/stages/publish.py::PublishStage",
        "app/models/stages/shared.py::find_declared_vs_computed_issues",
        "app/models/stages/shared.py::find_predicate_column_issues",
        "app/models/stages/shared.py::resolve_input_columns",
        "app/models/stages/stage_base.py::StageBase",
        "app/models/stages/stage_base.py::StageBase._config_columns_resolve",
        "app/models/stages/stage_base.py::StageBase._empty_tests_means_absent",
            "app/models/stages/stage_base.py::StageBase._schemas_declared",
        "app/models/stages/stage_base.py::StageBase.compute_definition_fingerprint",
        "app/models/stages/stage_base.py::StageBase.find_authored_code_block",
        "app/models/stages/stage_base.py::StageBase.find_config_column_issues",
            "app/models/stages/stage_base.py::StageBase.fingerprint_blocks",
        "app/models/stages/stage_base.py::StageBase.is_grain_and_order_preserving",
        "app/models/stages/stage_base.py::StageCommon",
        "app/models/stages/stage_base.py::is_grain_and_order_preserving",
        "app/models/stages/stage_tests.py::StageTest._keep_a_failure_claim_visible",
        "app/models/stages/stage_tests.py::build_stage_tests_model",
        "app/models/stages/stage_tests.py::validate_stage_tests",
        "app/models/stages/stage_tests.py::validate_test_rows",
        "app/models/stages/union.py::UnionConfig",
        "app/models/stages/union.py::find_union_column_issues",
            "app/models/table.py::TableRef",
        "app/models/workflow.py::Workflow.index_stages_by_id",
        "app/models/workflow.py::detect_cycle",
        "app/models/workflow.py::graph_issues",
        "app/models/workflow.py::sort_stages_by_dependency",
        "app/models/workflow.py::validate_edge_schemas",
        "app/models/workflow.py::validate_inputs_resolve",
        "app/models/workflow.py::validate_publish_is_terminal",
        "app/models/workflow.py::validate_workflow",
        "app/models/workflow.py::validate_workflow_draft",
        "app/runtime/_arch_tests/test_stages_no_cross_run_disk.py::find_persisted_write_call_offenders",
        "app/runtime/_arch_tests/test_takes_objects_not_dirs.py::find_banned_parameter_uses",
        "app/runtime/_arch_tests/test_takes_objects_not_dirs.py::find_function_parameters",
        "app/runtime/_arch_tests/test_takes_objects_not_dirs.py::test_find_banned_parameter_uses_ignores_similarly_named_local_variable",
        "app/runtime/_arch_tests/test_takes_objects_not_dirs.py::test_find_banned_parameter_uses_ignores_unrelated_root_suffixed_name",
        "app/runtime/cancellation.py::consume_cancel",
        "app/runtime/cancellation.py::request_cancel",
        "app/runtime/cancellation.py::reset",
        "app/runtime/context.py::RunContext",
        "app/runtime/context.py::RunContext.for_stages_outside_a_run",
        "app/runtime/context.py::RunContext.for_workflow_run",
        "app/runtime/context.py::RunContext.for_workflow_test_run",
        "app/runtime/context.py::RunContext.require_run_dir",
        "app/runtime/context.py::RunIdentity",
        "app/runtime/errors.py::HaltForReview",
        "app/runtime/errors.py::PreviewError",
        "app/runtime/errors.py::RunCancelled",
        "app/runtime/executor.py::_StageOutcome",
        "app/runtime/executor.py::_consume_cancel",
        "app/runtime/executor.py::_final_run_status",
        "app/runtime/executor.py::_finalize_run_manifest",
        "app/runtime/executor.py::_finalize_stage_output",
        "app/runtime/executor.py::_find_blocking_upstream",
        "app/runtime/executor.py::_flush_manifest",
        "app/runtime/executor.py::_gather_stage_inputs",
        "app/runtime/executor.py::_merge_stage_contribution",
        "app/runtime/executor.py::_persist_row_lineage",
        "app/runtime/executor.py::_raise_if_run_failed",
        "app/runtime/executor.py::_read_run_identity",
        "app/runtime/executor.py::_read_stage_contribution",
        "app/runtime/executor.py::_record_halt",
        "app/runtime/executor.py::_record_stage_error",
        "app/runtime/executor.py::_run_ordered_stages",
        "app/runtime/executor.py::_run_stage",
        "app/runtime/executor.py::_stage_output_already_produced",
        "app/runtime/executor.py::_stage_row_lineage",
        "app/runtime/executor.py::_summarize_output_schema_errors",
        "app/runtime/executor.py::_summarize_row_errors",
        "app/runtime/executor.py::run_subset",
        "app/runtime/llm.py::_compose_system",
        "app/runtime/llm.py::_record_usage",
        "app/runtime/llm.py::_run_agent",
        "app/runtime/llm.py::call_llm",
        "app/runtime/llm.py::call_llm_batch",
        "app/runtime/llm.py::render_prompt",
        "app/runtime/manifest.py::QueueStats",
        "app/runtime/manifest.py::RunManifest",
        "app/runtime/manifest.py::RunManifest._halted_at_to_list",
        "app/runtime/manifest.py::RunManifest.clear_halt",
        "app/runtime/manifest.py::RunManifest.record_dropped_columns",
        "app/runtime/manifest.py::RunManifest.settle_stage_records",
        "app/runtime/manifest.py::RunManifest.to_dict",
        "app/runtime/manifest.py::StageContribution",
        "app/runtime/manifest.py::StageErrorInfo",
        "app/runtime/manifest.py::StageRecord",
        "app/runtime/manifest.py::StageRecord.add_note",
        "app/runtime/manifest.py::StageRecord.record_with_status",
        "app/runtime/manifest.py::create_run_manifest",
        "app/runtime/manifest.py::load_manifest_model",
        "app/runtime/manifest.py::write_manifest",
        "app/runtime/options.py::agent_available",
        "app/runtime/options.py::require_agent_backend",
        "app/runtime/preview.py::_load_upstream_inputs",
        "app/runtime/preview.py::run_stage_preview",
        "app/runtime/runner.py::_merge_connector_params",
        "app/runtime/runner.py::apply_run_bindings",
        "app/runtime/runner.py::execute_run",
        "app/runtime/runner.py::prepare_run",
        "app/runtime/runner.py::resume_run",
        "app/runtime/runner.py::run_prepared",
        "app/runtime/runner.py::validate_stages_ready",
        "app/runtime/stage_tests.py::_build_frame",
        "app/runtime/stage_tests.py::_is_absent",
        "app/runtime/stage_tests.py::_judge_raise",
        "app/runtime/stage_tests.py::_select_cells",
        "app/runtime/stage_tests.py::_select_target_stages",
        "app/runtime/stage_tests.py::_sort_rows",
        "app/runtime/stage_tests.py::_validate_test_against_schemas",
        "app/runtime/stage_tests.py::_values_equal",
        "app/runtime/stage_tests.py::find_failing_stage_tests",
        "app/runtime/stage_tests.py::run_stage_tests",
        "app/runtime/stage_tests.py::run_tests_for_stage",
        "app/runtime/stages/execution.py::FrameHandler",
        "app/runtime/stages/execution.py::LLMTransformHandler",
        "app/runtime/stages/execution.py::PostMapRowMapper",
        "app/runtime/stages/execution.py::RowMapHandler",
        "app/runtime/stages/execution.py::StageHandler",
        "app/runtime/stages/execution.py::StageHandler.preserves_grain_and_order",
        "app/runtime/stages/execution.py::_InternalRowColumn",
        "app/runtime/stages/execution.py::_RowCaching",
        "app/runtime/stages/execution.py::_collect_internal_columns",
        "app/runtime/stages/execution.py::_collect_row_errors",
        "app/runtime/stages/execution.py::_collect_row_usage",
        "app/runtime/stages/execution.py::_compute_batched_rows",
        "app/runtime/stages/execution.py::_consume_cancel",
        "app/runtime/stages/execution.py::_finish_batched_frame",
        "app/runtime/stages/execution.py::_finish_mapped_frame",
        "app/runtime/stages/execution.py::_open_row_caching",
        "app/runtime/stages/execution.py::_order_by_input_position",
        "app/runtime/stages/execution.py::_project_onto_declared_columns",
        "app/runtime/stages/execution.py::_record_row_output",
        "app/runtime/stages/execution.py::_restore_input_columns_when_nothing_named_them",
        "app/runtime/stages/execution.py::_run_batched",
        "app/runtime/stages/execution.py::_run_row_mapper",
        "app/runtime/stages/execution.py::_strip_and_project",
        "app/runtime/stages/execution.py::_strip_internal_columns",
        "app/runtime/stages/execution.py::narrow_stage",
        "app/runtime/stages/execution.py::validate_registry_matches_model",
        "app/runtime/stages/frame_caching.py::FrameCaching",
        "app/runtime/stages/frame_caching.py::StageCacheKey",
        "app/runtime/stages/frame_caching.py::_note_on_contribution",
        "app/runtime/stages/frame_caching.py::note_skipped_caching",
        "app/runtime/stages/frame_caching.py::open_frame_caching",
        "app/runtime/stages/frame_caching.py::record_frame_output",
        "app/runtime/stages/human_review_queue.py::_QueueRowMapper",
        "app/runtime/stages/human_review_queue.py::_QueueRowMapper.finish_mapped_rows",
        "app/runtime/stages/human_review_queue.py::_compute_queueable_mask",
        "app/runtime/stages/human_review_queue.py::_find_pending_reviews",
        "app/runtime/stages/human_review_queue.py::_require_project_scope",
        "app/runtime/stages/human_review_queue.py::_write_pending_snapshot",
        "app/runtime/stages/human_review_queue.py::_write_queue_files",
        "app/runtime/stages/input_data.py::_read_geojson",
        "app/runtime/stages/input_data.py::preflight_input_data",
        "app/runtime/stages/llm_transform.py::_build_batch_reply_schema",
        "app/runtime/stages/llm_transform.py::_emit_failed",
        "app/runtime/stages/llm_transform.py::_emit_matched",
        "app/runtime/stages/llm_transform.py::_process_chunk",
        "app/runtime/stages/llm_transform.py::_render_batch_task",
        "app/runtime/stages/llm_transform.py::_run_chunks",
        "app/runtime/stages/llm_transform.py::_validate_batch_reply",
        "app/runtime/stages/llm_transform.py::make_llm_row_mapper",
        "app/runtime/stages/llm_transform.py::run_llm_batches",
        "app/runtime/stages/publish.py::_prepare_output_dir",
        "app/runtime/stages/publish.py::_resolve_trace_linker",
        "app/runtime/stages/publish.py::handle_publish",
        "app/runtime/stages/python_functions.py::handle_python_frame_function",
        "app/runtime/stages/python_functions.py::make_python_row_mapper",
        "app/runtime/trace_links.py::RowTraceLinker.build_row_trace_url",
        "app/seeds/bootstrap.py::ensure_store_configured",
        "app/seeds/capture_lobbying.py::capture_lobbying_bundle",
        "app/seeds/seed.py::discover_workflow_files",
        "app/seeds/seed.py::seed_all",
        "app/seeds/seed.py::seed_demo_data_if_enabled",
        "app/services/data_model.py::load_data_model",
        "app/services/data_model.py::write_data_model",
        "app/services/drafts.py::Draft",
        "app/services/drafts.py::DraftEdit",
        "app/services/drafts.py::DraftView",
        "app/services/drafts.py::SaveResult",
        "app/services/drafts.py::_load",
        "app/services/drafts.py::_parse_stage",
        "app/services/drafts.py::create_draft",
        "app/services/drafts.py::read_draft",
        "app/services/drafts.py::remove_draft_stage",
        "app/services/drafts.py::save_version",
        "app/services/drafts.py::set_draft_stage",
        "app/services/errors.py::WorkflowLoadError",
        "app/services/generation.py::_finish_data_model",
        "app/services/generation.py::_finish_stage_tests",
        "app/services/generation.py::start_generation",
        "app/services/generation.py::start_stage_test_generation",
        "app/services/loader.py::find_stage_file",
        "app/services/loader.py::list_stage_files",
        "app/services/loader.py::load_workflow",
        "app/services/loader.py::load_workflow_object",
        "app/services/loader.py::resolve_function_code",
        "app/services/node_review.py::NodeApprovalState",
        "app/services/node_review.py::_latest_decision_row",
        "app/services/node_review.py::approval_state_for",
        "app/services/node_review.py::approve_schema_library",
        "app/services/node_review.py::coverage_for",
        "app/services/node_review.py::data_model_state",
        "app/services/node_review.py::load_node_decisions",
        "app/services/node_review.py::node_content_hash",
        "app/services/node_review.py::node_decisions_path",
        "app/services/node_review.py::record_node_decision",
        "app/services/node_review.py::schema_library_content_hash",
        "app/services/node_review.py::strip_bookkeeping_keys",
        "app/services/project.py::DataModelStatus",
        "app/services/project.py::Project",
        "app/services/project.py::ProjectMeta",
        "app/services/project.py::ProjectState",
        "app/services/project.py::RunsSummary",
        "app/services/project.py::WorkflowFile",
        "app/services/project.py::WorkflowFile._drop_null_stage_keys",
        "app/services/project.py::WorkflowFile.to_json",
        "app/services/project.py::WorkflowStatus",
        "app/services/project.py::_load_compiled_stages",
        "app/services/project.py::_resolve_project_dir_to_write",
        "app/services/project.py::_runs_summary",
        "app/services/project.py::add_stage",
        "app/services/project.py::add_stages",
        "app/services/project.py::create_project",
        "app/services/project.py::describe_workflow",
        "app/services/project.py::edit_stage",
        "app/services/project.py::export_project",
        "app/services/project.py::find_document_path",
        "app/services/project.py::import_project",
        "app/services/project.py::list_projects",
        "app/services/project.py::project_meta",
        "app/services/project.py::project_state",
        "app/services/project.py::read_stage",
        "app/services/project.py::remove_stage",
        "app/services/project.py::sanitize_project_name",
        "app/services/project.py::write_project_meta",
        "app/services/run.py::RunStageDef",
        "app/services/run.py::_run_in_background",
        "app/services/run.py::read_run_status",
        "app/services/run.py::resolve_version",
        "app/services/run.py::resume",
        "app/services/run.py::start_run",
        "app/services/stage_edit.py::AddStagesResult",
        "app/services/stage_edit.py::_add_new_stage",
        "app/services/stage_edit.py::_apply",
        "app/services/stage_edit.py::_current_specs",
        "app/services/stage_edit.py::_find_blocking_input",
        "app/services/stage_edit.py::_find_description_issues",
        "app/services/stage_edit.py::_merge_patch",
        "app/services/stage_edit.py::_strip_bookkeeping_keys",
        "app/services/stage_edit.py::add_stage_spec",
        "app/services/stage_edit.py::add_stage_specs",
        "app/services/stage_edit.py::edit_stage_spec",
        "app/services/stage_edit.py::patch_stage_spec",
        "app/services/stage_edit.py::remove_stage_spec",
        "app/services/versioning.py::WorkflowVersion",
        "app/services/versioning.py::_invalid_version_document",
        "app/services/versioning.py::create_version_from_stages",
        "app/services/versioning.py::find_latest_version_id",
        "app/services/versioning.py::list_versions",
        "app/services/versioning.py::load_version",
        "app/services/versioning.py::load_version_stages",
        "app/services/versioning.py::publish_version",
        "app/services/versioning.py::validate_version_exists",
        "app/services/workflow_test.py::_frontier_stages",
        "app/services/workflow_test.py::_mint_run_id",
        "app/services/workflow_test.py::_resolve_workflow_test_version",
        "app/services/workflow_test.py::_run_frontier",
        "app/services/workflow_test.py::run_workflow_test",
        "app/services/workspace.py::configure_projects_dir_from_env",
        "app/services/workspace.py::list_project_names",
        "app/services/workspace.py::load_schemas",
        "app/services/workspace.py::project_workflow_summary",
        "app/services/workspace.py::projects_dir",
        "app/services/workspace.py::resolve_project_dir",
        "app/services/workspace.py::set_projects_dir",
        "app/web/chat_router.py::new_agent_session",
        "app/web/chat_router.py::post_message",
        "app/web/config.py::friendly_time",
        "app/web/diagrams.py::_build_workflow_node_label",
        "app/web/diagrams.py::_collect_er_fk_edges",
        "app/web/diagrams.py::_collect_table_fk_edges",
        "app/web/diagrams.py::_node_view",
        "app/web/diagrams.py::_render_er_column_row",
        "app/web/diagrams.py::_render_er_entity_block",
        "app/web/diagrams.py::_render_table_node_block",
        "app/web/diagrams.py::_render_workflow_node_lines",
        "app/web/diagrams.py::_resolve_stroke_line",
        "app/web/diagrams.py::build_schema_er_diagram",
        "app/web/diagrams.py::build_schema_table_graph",
        "app/web/loading.py::QueueFingerprints",
        "app/web/loading.py::StageListing",
        "app/web/loading.py::_build_project_card",
        "app/web/loading.py::_count_runs_with_manifest",
        "app/web/loading.py::_manifest_counts_as_run",
        "app/web/loading.py::_safe_component",
        "app/web/loading.py::build_llm_example",
        "app/web/loading.py::display_cell",
        "app/web/loading.py::list_file_inputs",
        "app/web/loading.py::list_projects",
        "app/web/loading.py::load_manifest",
        "app/web/loading.py::load_output_preview",
        "app/web/loading.py::load_output_row",
        "app/web/loading.py::load_output_table",
        "app/web/loading.py::load_queue_fingerprints",
        "app/web/loading.py::load_stages_or_empty",
        "app/web/loading.py::read_output_df",
        "app/web/loading.py::save_uploaded_input",
        "app/web/project_view.py::NavItem",
        "app/web/project_view.py::NextAction",
        "app/web/project_view.py::ShellState",
        "app/web/project_view.py::_next_action",
        "app/web/project_view.py::_runs_status",
        "app/web/project_view.py::_workflow_status",
        "app/web/project_view.py::build_nav",
        "app/web/project_view.py::shell_state",
        "app/web/routers/admin.py::_bundle_path",
        "app/web/routers/admin.py::admin_index",
        "app/web/routers/admin.py::export_project_route",
        "app/web/routers/admin.py::load_bundle",
        "app/web/routers/evals.py::_build_eval_index_rows",
        "app/web/routers/evals.py::_list_eval_runs_safely",
        "app/web/routers/evals.py::_read_eval_dataset_preview",
        "app/web/routers/evals.py::_render_eval_detail",
        "app/web/routers/evals.py::_resolve_eval_status",
        "app/web/routers/evals.py::evals_index",
        "app/web/routers/evals.py::trigger_eval_run",
        "app/web/routers/node_review.py::_find_generation_failure",
        "app/web/routers/node_review.py::_review_by_id",
        "app/web/routers/node_review.py::create_version_route",
        "app/web/routers/node_review.py::generation_session_status",
        "app/web/routers/node_review.py::node_decide",
        "app/web/routers/node_review.py::node_edit",
        "app/web/routers/node_review.py::node_generate_tests",
        "app/web/routers/node_review.py::node_review_partial",
        "app/web/routers/node_review.py::publish_version_route",
        "app/web/routers/node_review.py::review_status",
        "app/web/routers/project.py::_project_dir",
        "app/web/routers/project.py::_schema_json_map",
        "app/web/routers/project.py::_schema_library_approval",
        "app/web/routers/project.py::_schema_spec",
        "app/web/routers/project.py::approve_data_model",
        "app/web/routers/project.py::delete_project",
        "app/web/routers/project.py::edit_schema",
        "app/web/routers/project.py::generate_project",
        "app/web/routers/project.py::index",
        "app/web/routers/project.py::new_project_form",
        "app/web/routers/project.py::new_project_submit",
        "app/web/routers/project.py::project_data_model",
        "app/web/routers/project.py::project_document",
        "app/web/routers/project.py::project_overview",
        "app/web/routers/project.py::project_workflow",
        "app/web/routers/project.py::project_workflow_version",
        "app/web/routers/project.py::project_workflow_versions",
        "app/web/routers/project.py::version_stage_partial",
        "app/web/routers/runs.py::RunGraph",
        "app/web/routers/runs.py::_collect_bindings",
        "app/web/routers/runs.py::_collect_limits",
        "app/web/routers/runs.py::_read_bust_cache",
        "app/web/routers/runs.py::cancel_run_route",
        "app/web/routers/runs.py::resume_run_route",
        "app/web/routers/runs.py::run_inputs",
        "app/web/routers/runs.py::run_stage_rows",
        "app/web/routers/runs.py::run_stage_scratch_preview",
        "app/web/routers/runs.py::run_status",
        "app/web/routers/runs.py::runs_index",
        "app/web/routers/runs.py::stream_run_events",
        "app/web/routers/runs.py::trigger_run_of_version",
        "app/web/routers/runs.py::upload_input",
        "app/web/run_stage_panel.py::not_executed_panel",
        "app/web/stage_test_views.py::StageCertification",
        "app/web/stage_test_views.py::_carries_authored_code",
        "app/web/stage_test_views.py::build_certification",
        "app/web/stage_test_views.py::shape_test_views",
        "tests/arch/_helpers.py::find_class_body_assignment",
        "tests/arch/_helpers.py::find_class_body_function",
        "tests/arch/_helpers.py::find_dict_key_uses",
        "tests/arch/_helpers.py::find_imported_modules",
        "tests/arch/_helpers.py::find_numeric_get_defaults",
        "tests/arch/_helpers.py::find_subclasses_of",
        "tests/arch/import_allowlist.py::_find_governed_imports",
        "tests/arch/predicates.py::check_imports_are_stdlib_only",
        "tests/arch/predicates.py::check_no_dict_keys",
        "tests/arch/predicates.py::check_no_import",
        "tests/arch/predicates.py::find_banned_words",
        "tests/arch/predicates.py::find_check_prefixed_functions",
        "tests/arch/predicates.py::find_production_run_imports",
        "tests/arch/scope.py::find_source_files_under",
        "tests/arch/test_complexity_ratchet.py::FunctionComplexity",
        "tests/arch/test_complexity_ratchet.py::_measure_function_and_closures",
        "tests/arch/test_complexity_ratchet.py::find_app_source_files",
        "tests/arch/test_complexity_ratchet.py::find_functions_over_threshold",
        "tests/arch/test_complexity_ratchet.py::find_overload_stub_lines",
        "tests/arch/test_complexity_ratchet.py::index_by_identity",
        "tests/arch/test_complexity_ratchet.py::measure_function_complexities",
        "tests/arch/test_complexity_ratchet.py::test_find_app_source_files_excludes_vendor_but_includes_arch_tests",
        "tests/arch/test_complexity_ratchet.py::test_find_app_source_files_ignores_a_dot_directory_in_the_scanned_root_prefix",
        "tests/arch/test_complexity_ratchet.py::test_find_functions_over_threshold_raises_on_two_measurements_for_the_same_identity",
        "tests/arch/test_complexity_ratchet.py::test_index_by_identity_raise_offers_a_remedy_for_the_unrenamable_overload_case",
        "tests/arch/test_complexity_ratchet.py::test_measure_function_complexities_excludes_overload_stubs_but_keeps_the_implementation",
        "tests/arch/test_complexity_ratchet.py::test_measure_function_complexities_surfaces_both_blocks_of_a_platform_conditional_duplicate_name",
        "tests/arch/test_diagram_click_wiring.py::test_no_template_defines_its_own_mermaid_click_global",
        "tests/arch/test_errors_in_errors_module.py::_is_valid_errors_home",
        "tests/arch/test_errors_in_errors_module.py::find_base_class_names",
        "tests/arch/test_errors_in_errors_module.py::find_errors_module_offenders",
        "tests/arch/test_errors_in_errors_module.py::find_exception_class_defs",
        "tests/arch/test_errors_in_errors_module.py::test_find_errors_module_offenders_permits_an_exception_in_errors_py",
        "tests/arch/test_errors_in_errors_module.py::test_find_exception_class_defs_does_not_match_inside_a_word",
        "tests/arch/test_fastapi_only_in_web.py::find_disallowed_fastapi_importers",
        "tests/arch/test_fastapi_only_in_web.py::find_fastapi_imports",
        "tests/arch/test_fastapi_only_in_web.py::is_permitted_fastapi_importer",
        "tests/arch/test_fastapi_only_in_web.py::test_is_permitted_fastapi_importer_rejects_a_module_merely_prefixed_web",
        "tests/arch/test_file_io_declares_encoding.py::_opens_binary",
        "tests/arch/test_file_io_declares_encoding.py::find_encodingless_text_io",
        "tests/arch/test_file_io_declares_encoding.py::test_find_encodingless_text_io_allows_binary_path_open_method",
        "tests/arch/test_file_io_declares_encoding.py::test_find_encodingless_text_io_flags_open_whose_mode_is_not_a_literal",
        "tests/arch/test_file_size_ratchet.py::FileSize",
        "tests/arch/test_file_size_ratchet.py::_describe_new_violation",
        "tests/arch/test_file_size_ratchet.py::find_ratchet_violations",
        "tests/arch/test_import_graph.py::ModuleDegree",
        "tests/arch/test_import_graph.py::_TarjanState",
        "tests/arch/test_import_graph.py::_build_adjacency",
        "tests/arch/test_import_graph.py::_find_strongly_connected_components",
        "tests/arch/test_import_graph.py::_trace_cycle_within",
        "tests/arch/test_import_graph.py::_walk_for_cycle",
        "tests/arch/test_import_graph.py::find_app_internal_edges",
        "tests/arch/test_import_graph.py::find_fan_out_violations",
        "tests/arch/test_import_graph.py::find_import_cycles",
        "tests/arch/test_internal_columns_are_prefixed.py::read_declaration_table",
        "tests/arch/test_model_encapsulation.py::ProtectedAttributeRule",
        "tests/arch/test_model_encapsulation.py::_describe_mutated_protected_target",
        "tests/arch/test_model_encapsulation.py::_identify_receiver",
        "tests/arch/test_model_encapsulation.py::find_mutation_sites",
        "tests/arch/test_model_encapsulation.py::find_source_files",
        "tests/arch/test_module_docstring_ratchet.py::ModuleDocstring",
        "tests/arch/test_module_docstring_ratchet.py::find_python_files",
        "tests/arch/test_module_docstring_ratchet.py::find_ratchet_violations",
        "tests/arch/test_module_docstring_ratchet.py::measure_module_docstrings",
        "tests/arch/test_module_docstring_ratchet.py::test_find_python_files_recurses_into_a_subpackage_but_skips_exempt_parts",
        "tests/arch/test_module_docstring_ratchet.py::test_justified_exceptions_ships_empty_and_every_entry_carries_a_reason",
        "tests/arch/test_module_docstring_ratchet.py::test_measure_module_docstrings_counts_a_blank_line_inside_the_docstring",
        "tests/arch/test_no_html_in_python.py::find_html_tag_string_literals",
        "tests/arch/test_no_html_in_python.py::test_find_html_tag_string_literals_ignores_a_longer_word_sharing_the_tag_prefix",
        "tests/arch/test_repeated_string_literals.py::LiteralComparisonSite",
        "tests/arch/test_repeated_string_literals.py::find_compared_string_literals",
        "tests/arch/test_repeated_string_literals.py::find_repeated_literal_values",
        "tests/conftest.py::contribution_of",
        "tests/conftest.py::fresh_workspace",
        "tests/conftest.py::make_run_context",
        "tests/conftest.py::projects_root",
        "tests/conftest.py::reset_cancellation_registry",
        "tests/core/test_stage_cache.py::test_find_recorded_rows_skips_an_entry_that_recorded_no_output_row",
        "tests/core/test_stage_cache.py::test_stage_cache_record_stores_and_returns_a_none_output_row",
        "tests/core/test_store_config.py::test_both_defaults_land_under_the_same_relative_dir",
        "tests/core/test_store_config.py::test_pinning_the_db_path_carries_the_frames_root_with_it",
        "tests/core/test_store_config.py::unconfigured_stores",
        "tests/models/stages/test_aggregate_columns.py::test_column_declared_only_on_a_sibling_producer_is_not_enough",
        "tests/models/stages/test_aggregate_columns.py::test_where_unparseable_predicate_rejected",
            "tests/models/stages/test_join_columns.py::test_key_on_the_wrong_side_rejected",
        "tests/models/stages/test_shared.py::test_find_predicate_column_issues_turns_a_parse_failure_into_one_issue_not_raised",
        "tests/models/test_stage_config_fingerprint_fields.py::_config_block_fields",
        "tests/models/test_stage_config_fingerprint_fields.py::test_fingerprint_blocks_names_every_config_block_the_type_declares",
        "tests/models/test_stage_draft.py::test_a_draft_that_echoes_back_server_owned_fields_parses_and_records_them",
        "tests/models/test_stage_draft.py::test_a_stage_that_breaks_a_cross_field_rule_parses_as_a_draft_and_is_refused_by_stage",
        "tests/models/test_stage_draft.py::test_an_input_schema_round_trips_under_the_key_a_compiled_stage_spells",
        "tests/models/test_stage_draft.py::test_every_stage_class_shares_the_drafts_field_list",
        "tests/models/test_stage_draft.py::test_round_trip_covers_more_than_one_stage_type",
        "tests/models/test_stage_draft.py::test_stage_keeps_the_server_owned_fields_the_draft_drops",
        "tests/models/test_stage_draft.py::test_the_draft_carries_no_cross_field_validator_of_its_own",
        "tests/runtime/test_frame_cache.py::test_a_deliberate_opt_out_carries_no_note",
        "tests/runtime/test_frame_cache.py::test_a_run_without_project_scope_touches_the_cache_at_all",
        "tests/runtime/test_frame_cache.py::test_bust_cache_skips_the_read_but_still_re_pins",
        "tests/runtime/test_frame_cache.py::test_cache_false_reads_nothing_that_is_already_pinned",
        "tests/runtime/test_frame_cache.py::test_enrich_computes_every_run_and_records_nothing",
        "tests/runtime/test_frame_cache.py::test_no_frame_store_configured_computes_normally_and_caches_nothing",
        "tests/runtime/test_frame_cache.py::test_only_the_unbounded_frame_shaped_type_caches",
        "tests/runtime/test_frame_cache.py::test_reordering_the_input_rows_invalidates_the_cached_frame",
        "tests/runtime/test_frame_cache.py::test_the_registered_python_frame_function_replays_its_recorded_frame",
        "tests/runtime/test_hrq_cache.py::_alternating_src",
        "tests/runtime/test_hrq_cache.py::_every_outcome_src",
        "tests/runtime/test_hrq_cache.py::_put_approval",
        "tests/runtime/test_hrq_cache.py::_read_fingerprints",
        "tests/runtime/test_hrq_cache.py::test_a_cached_entry_holding_no_output_row_re_queues_the_row",
        "tests/runtime/test_hrq_cache.py::test_a_passed_through_row_round_trips_through_the_cache",
        "tests/runtime/test_hrq_cache.py::test_bust_cache_defers_every_queueable_row_despite_cached_decisions",
        "tests/runtime/test_hrq_cache.py::test_bust_cache_leaves_passed_through_rows_alone",
        "tests/runtime/test_hrq_cache.py::test_cache_is_read_once_per_stage_execution",
        "tests/runtime/test_hrq_cache.py::test_cancel_mid_queue_map_marks_the_stage_cancelled",
        "tests/runtime/test_hrq_cache.py::test_cancelled_execution_reports_no_queue_counts",
        "tests/runtime/test_hrq_cache.py::test_changing_the_filter_re_evaluates_a_passed_through_row",
        "tests/runtime/test_hrq_cache.py::test_fingerprint_matches_the_drivers_own_row_dict",
        "tests/runtime/test_hrq_cache.py::test_nullable_extension_dtype_cells_reach_the_reviewer_as_plain_numpy_values",
        "tests/runtime/test_hrq_cache.py::test_output_rows_stay_in_input_order",
        "tests/runtime/test_hrq_cache.py::test_queue_stats_hold_when_every_row_is_served_from_the_cache",
        "tests/runtime/test_hrq_cache.py::test_resume_reattaches_cached_decisions_written_via_the_seam",
        "tests/runtime/test_hrq_cache.py::test_resume_replays_the_runs_bust_cache",
        "tests/runtime/test_hrq_cache.py::test_snapshot_columns_match_original_upstream_columns_exactly",
        "tests/runtime/test_output_projection.py::_rating_stage",
            "tests/runtime/test_queue_filter_loud.py::test_a_cell_the_filter_cannot_answer_names_the_stage_and_the_filter",
        "tests/runtime/test_row_cache.py::_stub_call_llm_batch",
        "tests/runtime/test_row_cache.py::test_a_cached_llm_row_carries_no_usage",
        "tests/runtime/test_row_cache.py::test_a_post_map_mapper_still_gets_its_post_map_step",
        "tests/runtime/test_row_cache.py::test_a_run_without_project_scope_touches_the_cache_at_all",
        "tests/runtime/test_row_cache.py::test_batched_bust_cache_skips_the_read_but_re_pins",
        "tests/runtime/test_row_cache.py::test_batched_misses_that_were_not_adjacent_rejoin_their_own_rows",
        "tests/runtime/test_row_cache.py::test_bust_cache_skips_the_read_but_still_re_pins_the_entry",
        "tests/runtime/test_row_cache.py::test_cache_false_reads_nothing_that_is_already_pinned",
        "tests/runtime/test_row_cache.py::test_every_row_mapped_stage_type_runs_under_the_interceptor",
        "tests/runtime/test_row_cache.py::test_registered_python_row_function_replays_a_recorded_row_over_its_own_code",
        "tests/runtime/test_row_cache.py::test_run_llm_batches_computes_every_row_it_is_given",
        "tests/runtime/test_row_cache.py::test_the_scatter_puts_every_row_back_in_its_own_input_position",
        "tests/runtime/test_run_context.py::test_a_read_only_cache_allows_queue_auto_approve",
        "tests/runtime/test_run_context.py::test_a_writable_cache_rejects_queue_auto_approve",
        "tests/runtime/test_run_context.py::test_for_workflow_test_run_grants_scope_but_read_only",
        "tests/runtime/test_run_context.py::test_only_a_workflow_run_takes_bust_cache",
        "tests/runtime/test_run_context.py::test_run_context_has_no_project_scope_path",
        "tests/runtime/test_run_context.py::test_run_context_without_a_cache_rejects_bust_cache",
        "tests/runtime/test_run_log.py::test_a_batched_chunk_binds_the_input_rows_it_actually_covers",
        "tests/runtime/test_run_log.py::test_a_resumed_log_keeps_seq_equal_to_the_line_index",
        "tests/runtime/test_run_log.py::test_a_run_writes_its_lifecycle_spine_to_the_run_dir",
        "tests/runtime/test_run_log.py::test_a_tailer_resuming_at_the_pre_resume_cursor_sees_the_resumed_events",
        "tests/runtime/test_run_log.py::test_the_batched_path_logs_replayed_and_computed_rows_apart",
        "tests/test_admin_ui.py::workspace_root",
        "tests/test_agent_generate.py::_FakeEngine",
        "tests/test_agent_generate.py::_init_event",
        "tests/test_arch_predicates.py::test_predicate_flags_production_run_import",
            "tests/test_column_projection.py::_queue_test_ctx",
        "tests/test_dashboard_listing.py::_make_document_only_project",
        "tests/test_dashboard_listing.py::test_half_written_version_snapshot_fails_the_listing_loudly",
        "tests/test_dashboard_listing.py::test_unpublished_only_project_is_not_ready",
        "tests/test_dashboard_listing.py::test_versioned_project_is_ready_to_run",
        "tests/test_diagrams.py::test_cancelled_stage_gets_glyph_and_grey_stroke",
        "tests/test_diagrams.py::test_plain_stage_with_no_status_or_review_renders_the_bare_node",
        "tests/test_diagrams.py::test_typed_stage_input_renders_the_same_as_the_equivalent_draft_dict",
        "tests/test_diagrams.py::test_unrecognized_status_falls_back_to_review_belief_stroke",
        "tests/test_drafts.py::test_save_version_refuses_incomplete_workflow",
        "tests/test_drafts.py::test_set_stage_rejects_malformed_stage_missing_field",
        "tests/test_drafts.py::test_set_stage_rejects_unknown_connector_kind",
        "tests/test_eval.py::_py",
        "tests/test_eval_compatibility.py::_input_refs",
        "tests/test_eval_pages.py::demo_project",
        "tests/test_eval_pages.py::test_eval_detail_offers_a_version_select_newest_first_marking_unpublished",
        "tests/test_eval_pages.py::test_eval_detail_shows_no_versions_note_when_project_has_no_version",
        "tests/test_eval_pages.py::test_eval_detail_shows_pathway_compatibility_and_dataset",
        "tests/test_eval_pages.py::test_evals_index_lists_configs_with_status",
        "tests/test_eval_runner.py::project",
        "tests/test_eval_runner.py::test_run_eval_none_version_id_resolves_to_newest_overall",
        "tests/test_eval_runner.py::test_run_eval_raises_when_no_versions_exist_at_all",
        "tests/test_eval_runner.py::test_run_eval_raises_when_selected_version_does_not_exist",
        "tests/test_eval_runner.py::test_run_eval_scores_an_explicit_unpublished_version",
        "tests/test_eval_runner.py::test_run_eval_scores_the_pathway",
        "tests/test_eval_runner.py::test_run_eval_through_a_queue_stage_records_an_error_never_a_score",
        "tests/test_eval_runner.py::test_run_eval_writes_a_per_row_result_table",
        "tests/test_eval_runner.py::test_trigger_route_404s_when_selected_version_does_not_exist",
        "tests/test_eval_runner.py::test_trigger_route_scores_an_explicitly_selected_unpublished_version",
            "tests/test_eval_scoring.py::test_checked_column_clashing_with_override_is_read_from_output_prefixed_column",
        "tests/test_eval_store.py::test_latest_version_id_includes_unpublished_draft",
        "tests/test_eval_store.py::test_load_eval_run_ignores_sibling_invalid_run",
        "tests/test_frame_fingerprint.py::test_the_accessors_key_on_the_input_frames_themselves",
        "tests/test_frame_fingerprint.py::test_the_row_index_is_not_part_of_the_identity",
        "tests/test_frames.py::test_list_rows_gives_one_str_keyed_dict_per_row",
        "tests/test_generation_session.py::_FakeAgent",
        "tests/test_handler_execution.py::_MarksEveryRowAndKeepsTheFrame",
        "tests/test_handler_execution.py::test_a_plain_closure_mapper_needs_no_post_map_step",
        "tests/test_handler_registry.py::test_human_review_queue_maps_one_row_at_a_time_so_its_shared_counters_stay_correct",
        "tests/test_import_graph_report.py::_InconsistentReachabilityGraph",
        "tests/test_import_graph_report.py::_build_synthetic_graph",
        "tests/test_import_graph_report.py::_reachable_via_manual_bfs",
        "tests/test_import_graph_report.py::_run_script",
        "tests/test_import_graph_report.py::_write_synthetic_three_chain_package",
        "tests/test_journey_live_llm.py::offline_llm",
        "tests/test_journey_smoke.py::_point_examples_dir_at",
        "tests/test_journey_smoke.py::_workflow_stages",
        "tests/test_journey_smoke.py::assert_run_ok",
        "tests/test_llm_batch_rejoin.py::_clean",
        "tests/test_llm_usage.py::_fake_call_llm",
        "tests/test_manifest_model.py::test_a_pre_rename_manifest_fails_loudly_instead_of_reporting_zero",
        "tests/test_manifest_model.py::test_clear_halt_drops_halted_at_from_serialization",
        "tests/test_manifest_model.py::test_empty_contribution_is_the_default",
        "tests/test_manifest_model.py::test_fully_settled_manifest_round_trips_byte_identical",
        "tests/test_manifest_model.py::test_legacy_scalar_halted_at_is_normalized_to_a_list",
        "tests/test_manifest_model.py::test_manifest_round_trips_structurally",
        "tests/test_manifest_model.py::test_minted_manifest_omits_the_run_level_optionals",
        "tests/test_manifest_model.py::test_optional_fields_are_omitted_exactly_where_the_dict_code_omitted_them",
        "tests/test_manifest_model.py::test_recorded_tallies_survive_serialization_on_a_partial_manifest",
        "tests/test_mcp_add_stage_batch.py::test_a_batch_submitted_in_reverse_dependency_order_is_sorted_and_stored",
        "tests/test_mcp_add_stage_batch.py::test_a_cycle_among_the_submitted_stages_refuses_the_whole_batch",
        "tests/test_mcp_add_stage_batch.py::test_a_json_string_is_still_accepted_for_the_list",
        "tests/test_mcp_add_stage_batch.py::test_a_one_element_list_refuses_exactly_as_the_singular_call_did",
        "tests/test_mcp_add_stage_batch.py::test_a_stage_added_earlier_in_the_batch_satisfies_a_later_stage_edge",
        "tests/test_mcp_add_stage_batch.py::test_one_failure_keeps_the_independents_and_skips_only_its_dependency_cone",
        "tests/test_mcp_add_stage_batch.py::test_the_flattened_issues_still_carry_every_failure",
        "tests/test_mcp_add_stage_batch.py::test_two_stages_sharing_an_id_refuse_the_whole_batch",
        "tests/test_mcp_run_tools.py::_make_workflow_test_project",
        "tests/test_mcp_run_tools.py::test_run_workflow_test_delegates_and_reports_verdict",
        "tests/test_mcp_run_tools.py::test_run_workflow_translates_no_version_to_error",
        "tests/test_mcp_server.py::_write_compiled_workflow",
        "tests/test_mcp_server.py::test_add_stage_input_schema_omits_the_server_owned_fields",
        "tests/test_mcp_server.py::test_mcp_add_stage_drops_server_owned_fields_and_names_them",
        "tests/test_mcp_server.py::test_mcp_add_stage_refuses_an_invalid_stage_on_the_issues_channel",
        "tests/test_mcp_server.py::test_mcp_add_stage_refuses_to_invent_a_project",
        "tests/test_mcp_server.py::test_mcp_add_stage_reports_an_unloadable_workflow_as_issues",
        "tests/test_mcp_server.py::test_mcp_add_stage_still_refuses_an_unknown_field",
        "tests/test_mcp_server.py::test_mcp_save_version_omitting_the_parent_records_none",
        "tests/test_mcp_server.py::test_mcp_save_version_records_the_caller_supplied_parent",
        "tests/test_mcp_server.py::test_mcp_save_version_refuses_a_parent_that_does_not_exist",
        "tests/test_mcp_server.py::test_mcp_save_version_refuses_an_unloadable_working_copy",
        "tests/test_mcp_server.py::test_mcp_save_version_snapshots_the_working_copy_unpublished",
        "tests/test_mcp_server.py::test_mcp_stage_tools_report_an_unknown_stage_id_as_issues",
        "tests/test_named_schemas.py::test_named_schema_is_a_table_schema",
        "tests/test_node_types.py::test_every_stage_model_names_the_blocks_NODE_TYPES_advertises",
        "tests/test_node_types.py::test_every_stage_type_has_exactly_one_model_in_the_stage_union",
        "tests/test_persistence.py::test_concurrent_readers_and_writers_see_consistent_rows",
        "tests/test_persistence.py::test_concurrent_writers_all_land",
        "tests/test_project_export_import.py::test_a_bundle_from_before_per_type_stages_still_imports",
        "tests/test_project_export_import.py::test_a_non_null_foreign_config_block_is_still_refused",
        "tests/test_project_export_import.py::test_round_trip_through_json_reproduces_the_source_and_mints_a_version",
        "tests/test_project_persisted_model.py::test_bare_directory_does_not_block_creation",
        "tests/test_project_tools.py::_seed",
        "tests/test_project_tools.py::examples_root",
        "tests/test_publish_trace_links.py::test_a_link_emitted_into_published_html_resolves",
        "tests/test_reserved_column_namespace.py::test_a_plain_table_schema_is_indifferent_to_the_prefix",
        "tests/test_reserved_column_namespace.py::test_an_underscore_prefixed_key_nested_in_a_json_column_is_fine",
        "tests/test_run_cache_e2e.py::_append_input_row",
        "tests/test_run_cache_e2e.py::_probe_call",
        "tests/test_run_cache_e2e.py::_run_in_a_fresh_process",
        "tests/test_run_cache_e2e.py::_write_project",
        "tests/test_run_cache_e2e.py::test_a_first_run_of_a_fresh_project_replays_nothing",
        "tests/test_run_cache_e2e.py::test_bust_cache_recomputes_everything_and_leaves_the_cache_re_pinned",
        "tests/test_run_cache_e2e.py::test_editing_one_stages_function_body_invalidates_that_stage_alone",
        "tests/test_run_cache_e2e.py::test_editing_the_frame_stages_body_invalidates_only_the_frame_stage",
        "tests/test_run_cache_e2e.py::test_one_new_input_row_recomputes_only_that_row_but_the_whole_frame",
        "tests/test_run_cache_e2e.py::test_the_cache_survives_a_process_restart_and_a_change_of_directory",
        "tests/test_run_cancel.py::_three_stage_llm_project",
        "tests/test_run_cancel.py::test_a_cancelled_run_can_be_resumed_and_runs_to_completion",
        "tests/test_run_cancel.py::test_mid_run_cancel_preserves_the_completed_stages_output",
        "tests/test_run_cancel.py::test_mid_stage_cancel_marks_the_running_stage_cancelled_not_pending",
        "tests/test_run_cancel_route.py::_write_status_manifest",
        "tests/test_run_cancel_route.py::test_run_detail_page_offers_resume_for_a_cancelled_run",
        "tests/test_run_cancel_route.py::test_run_status_counts_include_a_cancelled_stage",
        "tests/test_run_events_stream.py::test_a_long_log_opens_on_the_tail_rather_than_replaying_all_of_it",
        "tests/test_run_graph_pinned_version.py::_drift_the_working_copy",
        "tests/test_run_graph_pinned_version.py::test_load_run_stages_raises_rather_than_falling_back",
        "tests/test_run_graph_pinned_version.py::test_run_with_a_null_pinned_version_shows_no_graph",
        "tests/test_run_graph_pinned_version.py::test_status_poller_graph_stays_on_the_pinned_version",
        "tests/test_run_log_client.py::test_a_drop_before_any_event_reconnects_to_the_tail_again",
        "tests/test_run_log_client.py::test_a_reconnect_resumes_at_the_cursor_and_never_duplicates",
        "tests/test_run_log_client.py::test_errors_only_surfaces_an_llm_error_while_llm_detail_is_off",
        "tests/test_run_loop_semantics.py::_filtered_queue_stage",
        "tests/test_run_loop_semantics.py::_five_item_load_stage",
        "tests/test_run_loop_semantics.py::_passthrough_stage",
            "tests/test_run_loop_semantics.py::_raising_stage",
        "tests/test_run_loop_semantics.py::test_cancel_after_a_halt_clears_halted_at_and_reports_cancelled",
        "tests/test_run_loop_semantics.py::test_error_and_halt_together_report_errors_but_keep_stage_awaiting_review",
        "tests/test_run_loop_semantics.py::test_error_blocks_transitive_downstream_in_a_chain",
        "tests/test_run_loop_semantics.py::test_error_in_one_fork_lets_the_independent_fork_finish",
        "tests/test_run_loop_semantics.py::test_halt_in_one_fork_lets_the_independent_fork_finish",
        "tests/test_run_loop_semantics.py::test_halted_queue_stages_item_counts_reach_the_run_manifest",
        "tests/test_run_loop_semantics.py::test_legacy_scalar_halted_at_manifest_renders_one_queue_link",
        "tests/test_run_loop_semantics.py::test_manifest_paths_are_posix_on_every_platform",
        "tests/test_run_loop_semantics.py::test_multi_halt_run_renders_the_full_halted_at_list_through_the_web_layer",
        "tests/test_run_loop_semantics.py::test_resume_after_error_reruns_the_errored_stage_and_its_downstream",
        "tests/test_run_loop_semantics.py::test_resume_pops_stale_halted_at_before_re_executing",
        "tests/test_run_loop_semantics.py::test_row_error_stage_blocks_downstream_and_resume_is_not_stale",
        "tests/test_run_loop_semantics.py::test_two_parallel_halts_each_block_only_their_own_downstream",
        "tests/test_run_service.py::_synchronous_background",
        "tests/test_run_service.py::project_dir",
        "tests/test_run_service.py::test_resolve_version_defaults_to_latest_published_and_raises_when_none",
        "tests/test_run_service.py::test_start_run_returns_run_id_and_writes_ok_manifest",
        "tests/test_run_stage_panel_not_executed.py::test_a_production_runs_input_stage_still_shows_its_run_detail",
        "tests/test_run_stage_panel_not_executed.py::test_input_stage_of_a_workflow_test_opens_instead_of_404ing",
        "tests/test_run_stage_views_pinned_version.py::_classify_stage",
        "tests/test_run_stage_views_pinned_version.py::_drift_the_working_copy",
        "tests/test_run_stage_views_pinned_version.py::_unpin_the_run",
        "tests/test_run_stage_views_pinned_version.py::test_scratch_preview_executes_the_stage_that_ran_not_the_working_copy",
        "tests/test_run_stage_views_pinned_version.py::test_scratch_preview_refuses_to_execute_an_unresolvable_version",
        "tests/test_run_trigger_bindings.py::_corrupt_version_document_with_relative_path",
        "tests/test_run_trigger_version.py::project_two_versions",
        "tests/test_run_trigger_version.py::project_versions_diff_paths",
        "tests/test_run_trigger_version.py::test_binding_provenance_uses_the_selected_versions_authored_path",
        "tests/test_run_trigger_version.py::test_run_form_hidden_when_no_published_version",
        "tests/test_run_trigger_version.py::test_run_inputs_endpoint_returns_the_selected_versions_inputs",
        "tests/test_run_trigger_version.py::test_run_picker_offers_only_published_versions",
        "tests/test_run_trigger_version.py::test_runs_page_renders_version_picker_latest_selected",
        "tests/test_runner.py::_add_frame_stage",
        "tests/test_runner.py::_llm_transform_project",
        "tests/test_runner.py::_output_schema_violation_project",
        "tests/test_runner.py::_seed_version",
        "tests/test_runner.py::_two_stage_project",
        "tests/test_runner.py::test_a_frame_stage_succeeds_with_no_frame_store_configured",
        "tests/test_runner.py::test_bust_cache_is_recorded_on_the_manifest",
        "tests/test_runner.py::test_create_version_rejects_invalid_working_copy",
        "tests/test_runner.py::test_invalid_workflow_never_becomes_a_version_and_run_never_pins_stale",
        "tests/test_runner.py::test_raise_if_run_failed_lists_halted_stages_as_readable_text",
        "tests/test_runner.py::test_resume_reapplies_run_bindings_for_a_pending_input_stage",
        "tests/test_runner.py::test_run_with_explicit_unpublished_id_fails_loudly",
        "tests/test_runner.py::test_run_with_no_published_version_fails_loudly",
        "tests/test_runner.py::test_run_without_a_version_fails_loudly",
        "tests/test_runner.py::test_the_documented_cli_runs_a_project_with_nothing_configured",
        "tests/test_runner.py::test_unpublished_latest_is_skipped_for_an_older_published_version",
        "tests/test_schema_capabilities.py::test_range_on_str_column_rejected_use_enum",
        "tests/test_schema_capabilities.py::test_spec_column_fields_read_off_the_model",
        "tests/test_schema_capabilities.py::test_subtract_nested_field_prose_difference_does_not_throw",
        "tests/test_schema_er_diagram.py::test_duplicate_fk_edges_are_deduped",
        "tests/test_schema_table_graph.py::test_no_fabricated_edges_without_references",
        "tests/test_sdk_engine.py::test_options_disable_every_builtin_tool_by_default",
        "tests/test_sdk_engine.py::test_options_load_no_mcp_servers_but_the_one_passed_in",
        "tests/test_sdk_engine.py::test_stream_turn_emits_the_cli_init_as_a_system_event_carrying_json",
        "tests/test_sdk_engine.py::test_stream_turn_surfaces_in_band_result_error",
        "tests/test_sdk_tools.py::_seed",
        "tests/test_sdk_tools.py::test_as_content_serializes_a_pydantic_model_to_its_fields",
        "tests/test_sdk_tools.py::test_draft_stage_input_schema_round_trips_in_alias_form",
        "tests/test_sdk_tools.py::test_set_draft_stage_rejects_malformed_stage_as_tool_error",
        "tests/test_seed_cli.py::test_seed_cli_subprocess_bootstraps_the_store_and_seeds",
        "tests/test_seed_lobbying.py::test_committed_lobbying_fixture_imports_and_validates_cleanly",
        "tests/test_seed_lobbying.py::test_seed_lobbying_issue_triage_loads",
        "tests/test_stage.py::_schema_spec",
        "tests/test_stage.py::test_data_template_required",
            "tests/test_stage.py::test_inputs_bare_id_shorthand_normalises_then_fails_on_the_missing_schema",
        "tests/test_stage.py::test_join_rejects_a_third_input",
        "tests/test_stage.py::test_llm_config_accepts_old_prompt_template_key_via_alias",
        "tests/test_stage.py::test_missing_config_block_is_a_structured_missing_error",
        "tests/test_stage.py::test_output_schema_issues_raise_at_stage_construction",
        "tests/test_stage.py::test_output_schema_issues_surface_in_draft_validation",
        "tests/test_stage.py::test_publish_requires_the_function_block_it_actually_runs",
        "tests/test_stage.py::test_stage_rejects_input_whose_schema_declares_no_columns",
        "tests/test_stage_certification.py::test_a_frame_function_is_certifiable_too",
        "tests/test_stage_certification.py::test_a_stage_whose_behaviour_is_not_code_is_not_applicable",
        "tests/test_stage_certification.py::test_any_non_passing_case_revokes_certification",
        "tests/test_stage_certification.py::test_filter_rows_with_no_description_is_undescribed_not_untestable",
        "tests/test_stage_certification.py::test_publish_carries_a_function_so_it_is_not_n_a",
        "tests/test_stage_edit.py::_seed_empty",
        "tests/test_stage_edit_requires_a_description.py::test_a_config_only_stage_needs_no_summary",
        "tests/test_stage_edit_requires_a_description.py::test_an_empty_corner_case_list_is_a_valid_answer",
        "tests/test_stage_edit_requires_a_description.py::test_editing_a_summary_away_is_refused",
        "tests/test_stage_edit_requires_a_description.py::test_omitting_corner_cases_entirely_is_refused",
        "tests/test_stage_test_generation.py::test_a_stage_with_no_summary_cannot_generate_examples",
        "tests/test_stage_test_generation.py::test_no_corner_cases_still_renders_a_task",
        "tests/test_stage_test_generation.py::test_task_never_contains_the_methodology_document",
        "tests/test_stage_test_generation_route.py::_FakeGeneratorAgent",
        "tests/test_stage_test_generation_route.py::_FakeGeneratorAgentNoAnswer",
        "tests/test_stage_test_generation_route.py::_seed_project",
        "tests/test_stage_test_generation_route.py::_valid_suite",
        "tests/test_stage_test_generation_route.py::client",
        "tests/test_stage_test_generation_route.py::test_generate_tests_maps_workflow_load_error_to_400",
            "tests/test_stage_test_generation_service.py::_FakeGeneratorAgent",
        "tests/test_stage_test_generation_service.py::_FakeGeneratorAgentNoAnswer",
        "tests/test_stage_test_generation_service.py::_seed_project",
        "tests/test_stage_test_generation_service.py::_suite_model",
        "tests/test_stage_test_generation_service.py::test_failed_generation_is_persisted_into_the_session",
        "tests/test_stage_test_generation_service.py::test_finish_stage_tests_preserves_null_cells",
        "tests/test_stage_test_generation_service.py::test_finish_with_empty_suite_raises",
        "tests/test_stage_test_generation_service.py::test_start_raises_before_session_for_non_python_stage",
        "tests/test_stage_test_model.py::test_a_test_omitting_expected_is_rejected",
        "tests/test_stage_test_model.py::test_failure_case_survives_the_spec_dict_round_trip",
        "tests/test_stage_test_model.py::test_row_function_failure_case_needs_no_expected_row",
        "tests/test_stage_test_model.py::test_stage_tests_model_accepts_an_empty_input_case",
        "tests/test_stage_test_model.py::test_stage_tests_model_rejects_a_row_omitting_a_column_another_row_supplies",
        "tests/test_stage_test_model.py::test_zero_expected_rows_is_not_a_failure_claim",
        "tests/test_test_runs_excluded_from_counts.py::test_runs_summary_excludes_test_runs_from_every_count",
        "tests/test_trace_endpoint.py::test_trace_view_says_reshaping_not_traceable",
        "tests/test_trace_helpers.py::write_run",
        "tests/test_trace_walk.py::_chain",
        "tests/test_union_and_filter_runtime.py::_load_stage",
        "tests/test_union_and_filter_runtime.py::test_a_row_mapper_that_may_not_drop_still_rejects_a_none_row",
        "tests/test_version_detail_page.py::test_run_this_version_400s_not_500s_on_unbound_input",
        "tests/test_versioning.py::_seed",
        "tests/test_versioning.py::test_create_version_freezes_coverage_from_node_decisions",
        "tests/test_versioning.py::test_create_version_from_stages_invalid_raises_and_writes_nothing",
        "tests/test_versioning.py::test_create_version_invalid_workflow_raises_and_writes_nothing",
        "tests/test_versioning.py::test_create_version_no_compiled_dir_raises_file_not_found",
        "tests/test_versioning.py::test_create_version_records_parent",
        "tests/test_versioning.py::test_create_version_returns_meta_and_round_trips",
        "tests/test_versioning.py::test_create_version_twice_within_a_second_overwrites",
        "tests/test_versioning.py::test_list_versions_errors_on_a_corrupt_document",
        "tests/test_versioning.py::test_stored_version_missing_published_reads_as_unpublished",
        "tests/test_versioning.py::test_versions_are_scoped_per_project",
        "tests/test_versions_page.py::test_publish_route_stamps_and_redirects_to_detail",
        "tests/test_versions_page.py::test_versions_list_shows_read_only_unpublished_status",
        "tests/test_web_smoke.py::demo_project",
        "tests/test_web_smoke.py::test_build_nav_groups_workflow_children",
        "tests/test_web_smoke.py::test_build_nav_status_tokens",
        "tests/test_web_smoke.py::test_display_cell_serializes_datetimes",
        "tests/test_web_smoke.py::test_project_page",
        "tests/test_web_smoke.py::test_project_shell_has_no_manual_edit_with_agent_control",
        "tests/test_web_smoke.py::test_sidebar_has_no_workflow_lock",
        "tests/test_web_smoke.py::test_sidebar_nests_versions_runs_evals_under_workflow",
        "tests/test_web_smoke.py::test_versions_page_uses_the_project_shell",
        "tests/test_web_smoke.py::test_workflow_page_points_to_versions_tab",
        "tests/test_web_smoke.py::test_workflow_page_run_links_to_the_runs_config_form",
        "tests/test_web_smoke.py::test_workflow_section_renders_the_graph",
        "tests/test_workflow.py::_consumer",
        "tests/test_workflow.py::_publish_upstream_stages",
        "tests/test_workflow.py::test_check_edge_schemas_raises_on_an_input_naming_no_stage",
            "tests/test_workflow.py::test_graph_issues_reports_a_dangling_input_instead_of_raising",
        "tests/test_workflow.py::test_graph_issues_reports_a_publish_upstream_instead_of_raising",
        "tests/test_workflow.py::test_parse_workflow_rejects_nonconformant_edge",
        "tests/test_workflow.py::test_sort_stages_by_dependency_ignores_inputs_from_outside_the_given_set",
        "tests/test_workflow_test_is_a_real_run.py::test_production_run_after_a_workflow_test_is_unaffected",
        "tests/test_workflow_test_is_a_real_run.py::test_workflow_test_replays_cached_rows_and_writes_no_new_entries",
        "tests/test_workflow_test_service.py::_seed",
        "tests/test_workflow_test_service.py::demo",
        "tests/test_workflow_test_service.py::test_workflow_test_auto_approves_a_queue_stage_in_memory",
        "tests/test_workflow_test_service.py::test_workflow_test_default_picks_newest_version_even_when_unpublished",
        "tests/test_workflow_test_service.py::test_workflow_test_limit_and_offset_slice_the_source",
        "tests/test_workflow_test_service.py::test_workflow_test_raises_when_no_source_stage",
        "tests/test_workflow_test_service.py::test_workflow_test_raises_when_no_versions_exist",
        "tests/test_workflow_test_service.py::test_workflow_test_reports_a_stage_error_as_failure",
        "tests/test_workflow_test_service.py::test_workflow_test_runs_an_explicit_unpublished_version",
        "tests/test_workflow_test_service.py::test_workflow_test_runs_publish_scoped_to_its_own_run_dir",
        "tests/test_workflow_test_service.py::test_workflow_test_writes_a_real_run_marked_is_test_run",
        "tests/test_xlsx_input.py::test_multi_sheet_selection_rejected_up_front",
    }
)

# Symbols over the ceiling only once the comment block above their first statement was
# folded into the same budget as their docstring — the change that made this rule count
# entrance prose in both syntaxes rather than docstrings alone. Same burn-down discipline
# as `_GRANDFATHERED`: entries may only be REMOVED, and a stale one fails loud. A symbol
# already listed above is not repeated here; it is exempt under either measure.
_GRANDFATHERED_ENTRANCE_PROSE: frozenset[str] = frozenset(
    {
        "app/agents/compiler/config.py::_build_editing_tools",
        "app/core/agent/agent.py::Agent.submit_answer",
        "app/core/agent/diagnostics.py::_find_tool_results",
        "app/core/agent/sdk_engine.py::ClaudeAgentSdkEngine._options",
        "app/core/agent/sdk_engine.py::ClaudeAgentSdkEngine.stream_turn",
        "app/core/frame_checks.py::_find_duplicate_row_groups",
        "app/core/frames.py::_is_date_cell",
        "app/core/frames.py::_is_int_cell",
        "app/core/persistence.py::_now_iso",
        "app/main.py::lifespan",
        "app/models/schema.py::Column.resolve_numeric_bounds",
        "app/models/stages/aggregate.py::AggregateConfig",
        "app/models/stages/human_review_queue.py::QueueConfig",
        "app/models/stages/human_review_queue.py::_find_added_column_collisions",
        "app/models/stages/human_review_queue.py::_find_reviewed_target_issues",
        "app/models/stages/human_review_queue.py::resolve_queue_config",
        "app/models/stages/input_data.py::Connector",
        "app/models/stages/llm_transform.py::LLMConfig",
        "app/models/stages/publish.py::PublishConfig",
        "app/models/stages/stage_tests.py::_find_test_frame_problems",
        "app/models/stages/stage_tests.py::validate_test_frames",
        "app/runtime/code.py::load_function",
        "app/runtime/context.py::RunContext._a_writable_cache_forbids_queue_auto_approve",
        "app/runtime/context.py::RunContext.attach_run_log",
        "app/runtime/executor.py::_execute_stages",
        "app/runtime/executor.py::_subset_ctx",
        "app/runtime/lineage.py::EdgeKind",
        "app/runtime/lineage.py::RowLineage",
        "app/runtime/lineage.py::RowLineage.from_frame",
        "app/runtime/lineage.py::RowLineage.shifted",
        "app/runtime/lineage.py::attach_row_lineage",
        "app/runtime/lineage.py::concatenated_inputs_lineage",
        "app/runtime/lineage.py::merged_inputs_lineage",
        "app/runtime/run_log.py::DetailSink.emit",
        "app/runtime/run_log.py::RunLog._drain",
        "app/runtime/run_log.py::RunLog.emit",
        "app/runtime/run_log.py::_count_logged_events",
        "app/runtime/run_log.py::read_events_since",
        "app/runtime/stage_tests.py::_compare",
        "app/runtime/stages/human_review_queue.py::PendingReview",
        "app/runtime/stages/human_review_queue.py::_approve_row",
        "app/runtime/stages/human_review_queue.py::_compute_queue_stats",
        "app/runtime/stages/human_review_queue.py::_defer_row",
        "app/runtime/stages/human_review_queue.py::_skip_row",
        "app/runtime/stages/human_review_queue.py::_write_fingerprint_sidecar",
        "app/runtime/stages/human_review_queue.py::make_human_review_mapper",
        "app/runtime/stages/human_review_queue.py::validate_reviewed_sources_present",
        "app/runtime/stages/input_data.py::_add_source_row_column",
        "app/runtime/stages/input_data.py::_read_dtype",
        "app/runtime/stages/input_data.py::_read_xlsx",
        "app/runtime/stages/join.py::_describe_cardinality_failure",
        "app/runtime/stages/join.py::handle_enrich",
        "app/runtime/stages/llm_transform.py::_build_chunk_processor",
        "app/runtime/stages/llm_transform.py::make_llm_row_mapper.map_row",
        "app/runtime/stages/row_events.py::emit_batched_row_outcomes",
        "app/runtime/stages/row_events.py::emit_cached_row",
        "app/runtime/stages/row_events.py::emit_row_outcome",
        "app/runtime/trace.py::_advance",
        "app/runtime/trace.py::_advance_via_lineage",
        "app/runtime/trace.py::_columns_parent_id",
        "app/runtime/trace.py::_is_row_preserving",
        "app/runtime/trace.py::_scalar",
        "app/runtime/trace.py::_split_spine",
        "app/runtime/trace.py::trace_row",
        "app/services/generation.py::start_review_guide_generation",
        "app/services/project.py::read_review_guide",
        "app/services/review.py::record_decision",
        "app/services/review.py::resolve_verdict",
        "app/services/run.py::_prepare",
        "app/services/run.py::read_pinned_version",
        "app/services/run_guide.py::_index_stages_in_execution_order",
        "app/services/versioning.py::_no_coverage",
        "app/services/versioning.py::find_latest_review_guide",
        "app/services/versioning.py::resolve_version_id",
        "app/web/config.py::RevalidatedStaticFiles",
        "app/web/config.py::relative_time",
        "app/web/queue_view.py::Lineage",
        "app/web/queue_view.py::QueuedColumn",
        "app/web/queue_view.py::ReviewedField",
        "app/web/queue_view.py::_as_cell_text",
        "app/web/queue_view.py::_as_option_text",
        "app/web/queue_view.py::_build_field_prefills",
        "app/web/queue_view.py::_build_review_items",
        "app/web/queue_view.py::_build_upstream_texts",
        "app/web/queue_view.py::_is_null",
        "app/web/queue_view.py::_require_recorded_output",
        "app/web/queue_view.py::_resolve_prefill",
        "app/web/queue_view.py::_subtract_reviewed_columns",
        "app/web/queue_view.py::build_lineage_urls",
        "app/web/queue_view.py::build_queue_page",
        "app/web/queue_view.py::describe_queued_columns",
        "app/web/queue_view.py::find_definition_drift",
        "app/web/queue_view.py::resolve_lineage",
        "app/web/queue_view.py::resolve_notes_label",
        "app/web/routers/review.py::_as_posted_text",
        "app/web/routers/review.py::_resolve_queue_row",
        "app/web/routers/review.py::_validate_reviewed_values",
        "app/web/routers/review.py::queue_decide",
        "app/web/routers/run_lineage.py::run_stage_lineage_panel",
        "app/web/routers/run_lineage.py::run_stage_row_trace",
        "app/web/routers/run_lineage.py::run_stage_row_trace_view",
        "app/web/routers/runs.py::_tail_run_events",
        "app/web/routers/runs.py::_tail_start_seq",
        "app/web/routers/runs.py::run_events_page",
        "app/web/routers/runs.py::run_stage_rows_csv",
        "app/web/run_header.py::build_live_view",
        "app/web/run_header.py::choose_run_cta",
        "app/web/run_header.py::list_artifact_links",
        "app/web/run_header.py::read_version_note",
        "app/web/run_index.py::build_run_index_rows",
        "app/web/stage_test_views.py::_find_new_output_columns",
        "app/web/trace_view.py::_transform_of",
        "app/web/trace_view.py::build_trace_view",
        "tests/arch/test_import_graph.py::_TarjanState.connect",
        "tests/arch/test_import_graph.py::test_find_import_cycles_reports_a_self_loop_within_a_larger_scc",
        "tests/arch/test_import_graph.py::test_find_import_cycles_reports_one_path_per_disjoint_cycle",
        "tests/arch/test_llm_models_are_pinned.py::test_str_is_the_wire_id",
        "tests/arch/test_llm_models_are_pinned.py::test_the_runtime_default_is_a_pinned_model",
        "tests/arch/test_tool_descriptions_name_available_tools.py::test_the_detector_sees_the_cross_references_the_descriptions_carry",
        "tests/conftest.py::pinned_stages",
        "tests/conftest.py::queue_added_columns",
        "tests/conftest.py::queue_columns",
        "tests/conftest.py::resumed_stages",
        "tests/core/test_stage_cache.py::test_compute_row_fingerprint_guards_array_valued_cells",
        "tests/core/test_stage_cache.py::test_old_shape_entry_fails_loudly_on_load",
        "tests/core/test_stage_cache.py::test_record_json_safes_both_rows",
        "tests/core/test_stage_cache.py::test_record_stores_under_the_passed_fingerprint_not_a_recomputed_one",
        "tests/models/stages/test_human_review_queue_columns.py::test_a_non_nullable_review_record_column_is_rejected",
        "tests/models/stages/test_human_review_queue_columns.py::test_a_review_record_name_reused_as_a_reviewed_target_is_rejected",
        "tests/models/stages/test_human_review_queue_columns.py::test_an_added_column_that_the_input_already_declares_is_rejected",
        "tests/models/stages/test_human_review_queue_columns.py::test_two_sources_mapping_to_the_same_target_are_rejected",
            "tests/models/test_stage_fingerprint.py::test_compute_definition_fingerprint_for_publish_reacts_to_function_code",
        "tests/models/test_stage_fingerprint.py::test_compute_definition_fingerprint_survives_a_stored_round_trip",
        "tests/runtime/test_enrich_expand_cardinality.py::test_enrich_preserves_subject_order_even_when_the_keys_are_unsorted",
        "tests/runtime/test_hrq_cache.py::test_a_modified_row_stays_in_its_own_position_carrying_the_human_score",
        "tests/runtime/test_hrq_cache.py::test_every_decided_row_is_emitted_with_only_the_declared_columns",
        "tests/runtime/test_hrq_cache.py::test_every_output_row_carries_a_verdict_covering_every_outcome",
        "tests/runtime/test_hrq_cache.py::test_input_fingerprint_matches_original_row_before_any_review_record_stamped",
        "tests/runtime/test_hrq_cache.py::test_queue_stats_count_every_row_the_reviewer_answered",
        "tests/runtime/test_hrq_declared_columns.py::_stage",
        "tests/runtime/test_hrq_declared_columns.py::test_a_queued_row_also_refuses_an_absent_source_column",
        "tests/runtime/test_hrq_declared_columns.py::test_a_source_column_absent_from_the_frame_raises",
        "tests/runtime/test_hrq_declared_columns.py::test_auto_approve_copies_the_source_value_under_the_approve_verdict",
        "tests/runtime/test_hrq_declared_columns.py::test_declared_names_are_the_only_columns_added",
        "tests/runtime/test_hrq_declared_columns.py::test_filtered_out_row_is_skipped_with_the_source_value_copied",
        "tests/services/test_review.py::_added_columns",
        "tests/test_agent_generate.py::test_answer_exposes_the_captured_submission_for_live_driving",
        "tests/test_agent_generate.py::test_failure_counts_an_unreadable_init_instead_of_reading_it_as_absent",
        "tests/test_agent_generate.py::test_failure_reads_the_tool_inventory_out_of_the_engines_init_event",
        "tests/test_agent_generate.py::test_failure_reports_a_tool_the_init_never_advertised",
        "tests/test_agent_generate.py::test_failure_separates_calls_the_model_emitted_from_calls_the_handler_saw",
        "tests/test_agent_generate.py::test_failure_shows_a_tool_less_completion_as_zero_calls_and_no_results",
        "tests/test_agent_generate.py::test_run_forwards_events_to_an_opted_in_caller_and_still_summarizes",
        "tests/test_agent_routes.py::test_chat_page_hides_composer_for_view_only_session",
        "tests/test_authoring_lifecycle_prompt.py::test_lifecycle_embeds_the_slice_verbatim",
        "tests/test_authoring_lifecycle_prompt.py::test_lifecycle_states_the_steps_and_their_gates",
        "tests/test_authoring_lifecycle_prompt.py::test_research_may_build_a_prototype_without_skipping_the_gates",
        "tests/test_authoring_lifecycle_prompt.py::test_slice_states_the_reader_and_the_why",
        "tests/test_chat_hidden_sessions.py::test_chat_index_excludes_hidden_sessions",
            "tests/test_column_projection.py::_src_scored",
            "tests/test_column_projection.py::test_llm_transform_declared_input_column_rides_through",
        "tests/test_column_projection.py::test_llm_transform_drops_undeclared_columns_including_former_hardcoded_ids",
        "tests/test_column_tightness.py::test_a_column_may_still_be_declared_loose_it_just_has_to_say_so",
        "tests/test_column_tightness.py::test_the_requirement_reaches_the_schema_library_the_data_model_agent_submits",
        "tests/test_column_tightness.py::test_the_requirement_reaches_the_submit_answer_tool_input_schema",
        "tests/test_eval.py::test_eval_config_no_key_or_input_columns_fields",
        "tests/test_eval.py::test_eval_config_rejects_stray_key_field",
        "tests/test_eval.py::test_expected_output_rejects_stray_expected_field",
        "tests/test_eval.py::test_human_review_queue_is_grain_and_order_preserving",
        "tests/test_eval.py::test_joins_and_aggregate_change_grain",
        "tests/test_eval.py::test_publish_not_grain_and_order_preserving",
        "tests/test_eval_compatibility.py::test_coverage_check_rejects_bare_name_on_a_conflicting_column",
        "tests/test_eval_compatibility.py::test_expected_output_column_not_in_target_schema",
        "tests/test_eval_compatibility.py::test_get_injected_columns_raises_for_checked_column_not_on_target",
        "tests/test_eval_compatibility.py::test_override_stage_has_no_output_schema",
        "tests/test_eval_compatibility.py::test_reference_override_stage_equals_target_stage",
        "tests/test_eval_compatibility.py::test_stages_list_has_a_structural_problem",
        "tests/test_eval_compatibility.py::test_target_not_reachable_from_override_is_broken",
        "tests/test_friendly_time_client.py::test_an_unparseable_datetime_yields_nothing_to_paint",
        "tests/test_friendly_time_client.py::test_past_a_week_the_date_itself_is_more_use_than_a_count",
        "tests/test_friendly_time_client.py::test_the_previous_calendar_day_reads_as_yesterday_with_the_clock_time",
        "tests/test_handler_execution.py::test_internal_marker_columns_never_reach_output_even_without_an_output_schema",
        "tests/test_handler_execution.py::test_row_driver_empty_input",
        "tests/test_handler_execution.py::test_row_driver_empty_input_reports_no_dropped_columns_when_projecting",
        "tests/test_handler_execution.py::test_row_driver_ignores_cancellation_when_ctx_has_no_run_identity",
        "tests/test_handler_execution.py::test_row_driver_parallel_branch_raises_run_cancelled_when_pre_requested",
        "tests/test_import_graph_report.py::test_cli_rejects_root_and_markdown_together",
        "tests/test_import_graph_report.py::test_compute_import_graph_metrics_on_the_real_repo_matches_the_arch_gate",
        "tests/test_import_graph_report.py::test_compute_propagation_cost_percent_on_a_three_chain",
        "tests/test_input_bindings.py::test_binding_connectorless_stage_rejected",
        "tests/test_input_bindings.py::test_invalid_merged_params_rejected_naming_the_stage",
        "tests/test_input_bindings.py::test_non_dict_binding_rejected_with_stage_id",
        "tests/test_input_bindings.py::test_read_input_data_names_the_stage_when_no_path_is_bound",
        "tests/test_input_data_declared_types.py::_xlsx_cells",
        "tests/test_input_data_declared_types.py::test_a_compact_yyyymmdd_xlsx_cell_declared_date_is_not_read_as_a_number",
        "tests/test_input_data_declared_types.py::test_a_real_excel_date_declared_date_survives_the_str_pin",
        "tests/test_input_data_declared_types.py::test_an_empty_xlsx_cell_declared_str_stays_null",
        "tests/test_input_data_declared_types.py::test_an_xlsx_text_cell_declared_str_keeps_its_zero_padding",
        "tests/test_input_data_declared_types.py::test_an_xlsx_text_cell_declared_str_keeps_the_digits_it_was_written_with",
        "tests/test_input_data_declared_types.py::test_bare_read_would_have_lost_them",
        "tests/test_input_data_declared_types.py::test_compact_yyyymmdd_date_is_not_read_as_a_number",
        "tests/test_input_data_declared_types.py::test_json_lines_list_column_arrives_as_a_real_list",
        "tests/test_input_data_declared_types.py::test_json_lines_str_column_keeps_its_zero_padding",
        "tests/test_input_data_declared_types.py::test_list_column_of_numeric_looking_values_keeps_its_zero_padding",
        "tests/test_input_data_declared_types.py::test_missing_output_schema_falls_back_to_plain_inference",
        "tests/test_llm_batch_rejoin.py::test_anomaly_is_thrown_back_and_recovers_on_retry",
        "tests/test_llm_batch_rejoin.py::test_batched_chunk_failure_reports_no_marker_as_a_dropped_column",
        "tests/test_llm_batch_rejoin.py::test_batched_run_reports_only_user_columns_as_dropped",
        "tests/test_llm_batch_rejoin.py::test_duplicate_number_same_length_fails_whole_chunk",
        "tests/test_llm_batch_rejoin.py::test_extra_unknown_number_fails_whole_chunk",
        "tests/test_llm_batch_rejoin.py::test_matched_by_row_number_not_reply_order",
        "tests/test_llm_transform_spec.py::test_timeout_with_empty_message_is_captured_and_labeled",
        "tests/test_llm_usage.py::test_failed_row_still_records_the_tokens_it_spent",
        "tests/test_llm_usage.py::test_run_manifest_records_stage_llm_usage",
        "tests/test_node_type_notes.py::test_corner_cases_note_reaches_every_code_carrying_type",
        "tests/test_node_type_notes.py::test_hrq_note_names_every_queue_field_that_adds_a_column",
        "tests/test_node_type_notes.py::test_hrq_note_names_the_decision_values_the_runtime_actually_emits",
        "tests/test_node_type_notes.py::test_summary_budget_note_states_the_limit_the_write_path_refuses_on",
        "tests/test_persistence.py::test_load_of_a_record_without_timestamps_fills_defaults_via_factory",
        "tests/test_persistence.py::test_persistedmodel_config_mirrors_base",
        "tests/test_persistence.py::test_save_advances_updated_at_but_not_created_at",
        "tests/test_project_tools.py::test_write_review_guide_rejects_an_invented_field",
        "tests/test_queue_view.py::_queue_stage",
        "tests/test_queue_view.py::test_lineage_links_the_single_upstream_stage_at_the_sidecar_ordinal",
        "tests/test_queue_view.py::test_the_context_table_omits_the_columns_under_review",
        "tests/test_queue_view.py::test_the_notes_label_prefers_the_declared_description",
        "tests/test_review_routes.py::_build_and_halt",
        "tests/test_review_routes.py::_build_and_halt_bool_queue",
        "tests/test_review_routes.py::_decide_a_temporal_row",
        "tests/test_review_routes.py::_decide_data",
        "tests/test_review_routes.py::_described_review_stage",
        "tests/test_review_routes.py::_drift_the_review_stage",
        "tests/test_review_routes.py::_empty_string_row_function_stage",
        "tests/test_review_routes.py::_every_column_reviewed_stage",
        "tests/test_review_routes.py::_find_selected_option",
        "tests/test_review_routes.py::_labelled_row_function_stage",
            "tests/test_review_routes.py::_put_cached_decision",
        "tests/test_review_routes.py::_review_stage",
        "tests/test_review_routes.py::_score_stage",
            "tests/test_review_routes.py::test_a_bool_select_opens_on_the_recorded_value_of_a_decided_row",
        "tests/test_review_routes.py::test_a_decided_card_disables_its_openers_and_offers_a_secondary_cta",
        "tests/test_review_routes.py::test_a_non_nullable_bool_select_opens_on_the_ai_value",
        "tests/test_review_routes.py::test_a_null_bool_ai_value_is_never_rendered_as_false",
        "tests/test_review_routes.py::test_a_queue_directly_on_input_data_renders_and_links_to_that_stage",
        "tests/test_review_routes.py::test_a_queue_whose_upstream_is_not_an_llm_transform_renders_and_links",
        "tests/test_review_routes.py::test_a_reviewed_value_is_read_only_until_its_edit_button_is_pressed",
        "tests/test_review_routes.py::test_a_temporal_control_opens_on_the_recorded_value_of_a_decided_row",
        "tests/test_review_routes.py::test_an_empty_string_cell_is_not_printed_as_a_null",
        "tests/test_review_routes.py::test_decide_accepts_an_untouched_notes_box_as_no_note",
        "tests/test_review_routes.py::test_e2e_decide_every_verdict_then_resume_completes",
        "tests/test_review_routes.py::test_queue_page_gates_the_items_behind_the_reviewer_name",
        "tests/test_review_routes.py::test_queue_page_prefills_a_decided_row_from_the_recorded_value",
        "tests/test_review_routes.py::test_queue_page_states_the_drift_and_renders_no_items",
        "tests/test_review_routes.py::test_the_card_renders_the_described_queued_row_and_its_review_section",
        "tests/test_review_routes.py::test_the_closed_field_displays_exactly_what_it_will_submit",
        "tests/test_review_routes.py::test_unlocking_a_decided_card_records_a_new_verdict_on_resubmit",
        "tests/test_row_function.py::test_row_function_rejects_multiple_inputs",
        "tests/test_row_slicing.py::test_limit_caps_the_rows_a_frame_handler_is_given",
        "tests/test_row_slicing.py::test_limit_keeps_the_row_mapper_off_the_rows_past_the_cap",
        "tests/test_row_slicing.py::test_union_lineage_counts_from_the_first_row_the_stage_actually_read",
        "tests/test_run_events_stream.py::test_an_explicit_from_seq_still_wins_over_the_tail_default",
        "tests/test_run_events_stream.py::test_an_interrupted_run_ends_the_stream_instead_of_hanging",
        "tests/test_run_graph_pinned_version.py::_input_stage",
        "tests/test_run_header.py::test_a_completed_run_offers_its_outputs_and_no_imperative_button",
        "tests/test_run_header.py::test_a_run_both_halted_and_failed_leads_with_the_review",
        "tests/test_run_loop_semantics.py::test_resume_of_a_run_with_no_pinned_version_fails_loudly",
        "tests/test_run_rows.py::_accented_df",
        "tests/test_run_rows.py::test_csv_download_opens_with_a_utf8_byte_order_mark",
        "tests/test_run_rows.py::test_csv_download_reimports_without_the_mark_in_a_column_name",
        "tests/test_run_stage_diff_panel.py::test_a_capped_filter_page_counts_input_rows_not_output_rows",
        "tests/test_run_stage_diff_panel.py::test_a_one_input_stage_names_its_only_input_without_a_base_marker",
        "tests/test_run_stage_diff_panel.py::test_a_second_input_lengthens_the_input_stack_without_moving_the_output",
        "tests/test_run_stage_diff_panel.py::test_every_frame_unit_links_the_raw_view_not_another_diff",
        "tests/test_run_stage_diff_panel.py::test_raw_1_forces_the_plain_table_and_says_which_view_it_is",
        "tests/test_run_stage_diff_panel.py::test_the_data_pane_keeps_the_input_row_picker",
        "tests/test_run_stage_diff_panel.py::test_the_diff_page_leaves_the_view_toggle_and_the_csv_to_the_header",
        "tests/test_run_stage_diff_panel.py::test_the_full_rows_diff_keeps_the_row_numbers_and_expandable_cells",
        "tests/test_run_stage_diff_panel.py::test_the_header_gives_every_frame_its_own_labelled_unit",
        "tests/test_run_status.py::test_css_class_pattern_renders_bare_not_qualified",
        "tests/test_runner.py::test_a_limited_stage_is_not_failed_by_a_duplicate_row_it_never_reads",
        "tests/test_runner.py::test_distinct_input_rows_pass",
        "tests/test_runner.py::test_duplicate_input_rows_fail_the_stage",
        "tests/test_runner.py::test_limit_on_a_source_stage_caps_the_rows_it_loads",
        "tests/test_runner.py::test_llm_generation_failure_surfaces_as_error_status_not_raised",
        "tests/test_runner.py::test_offset_makes_the_trace_land_on_the_true_upstream_row",
        "tests/test_runner.py::test_output_missing_a_declared_column_errors_the_stage_and_blocks_downstream",
        "tests/test_runner.py::test_output_validation_error_other_than_a_missing_column_also_errors_the_stage",
        "tests/test_runner.py::test_per_run_limit_and_offset_slice_and_are_recorded",
        "tests/test_runner.py::test_run_subset_preserves_partial_work_in_the_manifest_on_a_mid_frontier_error",
        "tests/test_runner.py::test_run_subset_surfaces_the_real_row_failure_message",
        "tests/test_runner.py::test_warning_only_output_report_does_not_error_the_stage",
        "tests/test_runs_index_listing.py::test_a_workflow_test_run_is_listed_and_reported_as_a_difference",
        "tests/test_runs_index_listing.py::test_an_unresolvable_pinned_version_says_so_instead_of_a_message",
        "tests/test_runs_index_listing.py::test_the_row_names_the_input_files_by_basename_only",
        "tests/test_schema_capabilities.py::test_find_unsatisfied_columns_allows_nullable_requirement_fed_by_non_null_producer",
        "tests/test_schema_capabilities.py::test_is_subset_of_uses_exact_nullability",
        "tests/test_schema_capabilities.py::test_subtract_strict_false_does_not_throw_on_spec_delta",
        "tests/test_stage.py::test_llm_transform_rejects_input_with_no_declared_schema",
        "tests/test_stage.py::test_llm_transform_rejects_spaced_double_braced_input_column",
        "tests/test_stage.py::test_model_enum_rejects_unversioned_alias",
        "tests/test_stage.py::test_python_function_inline_code_must_compile",
        "tests/test_stage.py::test_queue_needs_no_hash_source_declared",
        "tests/test_stage.py::test_stage_rejects_input_that_declares_no_schema",
        "tests/test_stage.py::test_weighted_formula_cut",
        "tests/test_stage_diff.py::test_a_filter_whose_sidecar_ordinals_do_not_increase_yields_no_diff",
        "tests/test_stage_diff.py::test_a_frame_function_gets_no_diff_even_at_matching_row_counts",
        "tests/test_stage_diff.py::test_a_reference_frame_that_will_not_read_is_listed_without_a_row_count",
        "tests/test_stage_diff.py::test_an_enrich_diffs_against_its_subject_input_not_its_reference",
        "tests/test_stage_diff.py::test_the_column_spine_is_the_input_frame_with_the_added_columns_after_it",
        "tests/test_stage_diff.py::test_the_row_budget_windows_the_input_frame_of_a_filter_diff",
        "tests/test_stage_edit.py::test_add_stage_still_refuses_when_the_existing_workflow_is_unloadable",
        "tests/test_stage_edit.py::test_edit_that_breaks_the_workflow_graph_is_rejected",
        "tests/test_stage_edit.py::test_remove_stage_rejected_when_a_downstream_depends_on_it",
        "tests/test_stage_edit_requires_a_description.py::test_the_field_description_states_the_limit_this_path_refuses_on",
        "tests/test_stage_schema_descriptions.py::test_connector_params_documents_optional_absolute_path_and_bans_invention",
        "tests/test_stage_schema_descriptions.py::test_llm_transform_notes_document_the_additive_rule",
        "tests/test_stage_summary_panel.py::test_a_summary_does_not_change_what_the_stage_computes",
        "tests/test_stage_test_model.py::test_stage_tests_model_accepts_repeated_expected_rows_under_no_key",
        "tests/test_stage_test_model.py::test_stage_without_tests_serializes_without_tests_key",
        "tests/test_stage_test_runner.py::test_failure_case_is_error_when_the_step_raises_something_else",
        "tests/test_stage_test_runner.py::test_failure_case_returning_a_non_dataframe_is_mismatch_not_crash",
        "tests/test_stage_test_runner.py::test_failure_case_returning_zero_rows_is_mismatch_not_passed",
        "tests/test_stage_test_runner.py::test_failure_case_skips_expected_row_schema_checks_but_not_its_inputs",
        "tests/test_stage_test_runner.py::test_frame_function_output_order_does_not_matter",
        "tests/test_stage_test_runner.py::test_frame_function_returning_none_is_error_not_crash",
        "tests/test_stage_test_runner.py::test_inline_code_raises_step_refused_without_importing_it",
        "tests/test_stage_test_runner.py::test_multi_input_frame_positional_order_is_declared_order",
        "tests/test_stage_test_runner.py::test_nan_output_matches_expected_none",
        "tests/test_stage_test_runner.py::test_omitted_column_in_expected_row_claims_none",
        "tests/test_store_neutral.py::test_list_sessions_returns_newest_first",
        "tests/test_trace_endpoint.py::test_trace_endpoint_encodes_nan_and_infinity_as_null",
        "tests/test_trace_helpers.py::test_is_row_preserving_matches_the_model_classification",
        "tests/test_trace_join_branches.py::_join_run",
        "tests/test_trace_join_branches.py::test_an_unmatched_row_has_one_parent_and_no_branch",
        "tests/test_trace_join_branches.py::test_branches_reach_the_render_payload",
        "tests/test_trace_join_branches.py::test_columns_new_is_only_what_the_join_added",
        "tests/test_trace_join_branches.py::test_contribution_parents_are_never_walked_into",
        "tests/test_trace_join_branches.py::test_expand_records_the_subject_row_each_fanned_out_row_came_from",
        "tests/test_trace_join_branches.py::test_handler_lineage_reaches_the_executor_channel",
        "tests/test_trace_join_branches.py::test_promoting_a_branch_is_just_another_trace",
        "tests/test_trace_join_branches.py::test_spine_follows_the_right_side_when_only_it_matched",
        "tests/test_trace_serialize.py::test_trace_to_dict_turns_non_finite_floats_into_null",
        "tests/test_trace_walk.py::test_human_review_queue_traces_positionally",
        "tests/test_trace_walk.py::test_llm_transform_traces_positionally",
        "tests/test_trace_walk.py::test_mismatch_deeper_in_chain_stops_at_the_right_step",
        "tests/test_trace_walk.py::test_rowcount_mismatch_on_preserving_stage_stops_defensively",
        "tests/test_union_and_filter_runtime.py::test_trace_follows_lineage_after_a_limit_caps_what_the_filter_reads",
        "tests/test_workflow.py::test_check_edge_schemas_clean_when_input_is_a_projection",
        "tests/test_workflow.py::test_check_edge_schemas_clean_when_producer_non_null_feeds_nullable_requirement",
        "tests/test_workflow_clean_state.py::test_a_workflow_that_does_not_load_claims_nothing",
        "tests/test_xlsx_input.py::test_source_row_column_holds_true_sheet_row_numbers",
    }
)

# Post-rule exceptions, each mapped to the written reason it earned one. Separate
# from `_GRANDFATHERED` on purpose: that set is a burn-down of pre-existing debt,
# this dict is a deliberate, argued carve-out. Ships EMPTY and should stay very
# rare — the normal remedy is cutting the prose to one short sentence, or moving the
# content to docs/ and referencing the file from the code.
_JUSTIFIED_EXCEPTIONS: dict[str, str] = {}


@dataclass(frozen=True)
class SymbolEntranceProse:
    path: str
    line: int
    symbol: str
    docstring_chars: int
    comment_chars: int

    @property
    def prose_chars(self) -> int:
        return self.docstring_chars + self.comment_chars


def measure_symbol_entrance_prose(paths: list[Path], repo_root: Path) -> list[SymbolEntranceProse]:
    measurements: list[SymbolEntranceProse] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        measurements.extend(
            _measure_children(
                ast.parse(text),
                path.relative_to(repo_root).as_posix(),
                prefix="",
                overload_lines=find_overload_stub_lines(text),
                comment_chars_by_line=read_comment_chars_by_line(text),
            )
        )
    return measurements


def index_by_symbol(measurements: list[SymbolEntranceProse]) -> dict[str, SymbolEntranceProse]:
    """Raises on a duplicate symbol; a ``@property``/``@x.setter`` pair collides."""
    by_symbol: dict[str, SymbolEntranceProse] = {}
    for measurement in measurements:
        key = f"{measurement.path}::{measurement.symbol}"
        if key in by_symbol:
            raise ValueError(
                f"entrance-prose ratchet: two symbols carrying prose resolve to {key} "
                f"(lines {by_symbol[key].line} and {measurement.line}) — the rule keys by "
                "path::Qualified.name and cannot tell them apart, so one would be measured "
                "and the other silently dropped. Give one a distinct name, or — where that "
                "is not an option, as with a @property/@x.setter pair — amend the identity "
                "key in tests/arch/test_docstring_length_ratchet.py"
            )
        by_symbol[key] = measurement
    return by_symbol


def find_ratchet_violations(
    measurements: list[SymbolEntranceProse],
    grandfathered: frozenset[str],
    entrance_prose_grandfathered: frozenset[str],
    exceptions: dict[str, str],
) -> list[str]:
    """Over the ceiling and unlisted, plus every stale grandfathered/exception entry."""
    by_symbol = index_by_symbol(measurements)
    offenders = [
        _describe_violation(by_symbol[key])
        for key in sorted(by_symbol)
        if by_symbol[key].prose_chars > _PROSE_CHAR_CEILING
        and key not in grandfathered
        and key not in entrance_prose_grandfathered
        and key not in exceptions
    ]
    for listed, list_name in (
        (grandfathered, "_GRANDFATHERED"),
        (entrance_prose_grandfathered, "_GRANDFATHERED_ENTRANCE_PROSE"),
        (frozenset(exceptions), "_JUSTIFIED_EXCEPTIONS"),
    ):
        offenders += [
            _describe_stale_entry(key, list_name)
            for key in sorted(listed)
            if _is_stale(key, by_symbol)
        ]
    return offenders


def test_entrance_prose_does_not_exceed_the_ratchet() -> None:
    measurements = measure_symbol_entrance_prose(
        find_governed_files(_APP_ROOT, _TESTS_ROOT), _REPO_ROOT
    )
    offenders = find_ratchet_violations(
        measurements, _GRANDFATHERED, _GRANDFATHERED_ENTRANCE_PROSE, _JUSTIFIED_EXCEPTIONS
    )
    assert not offenders, (
        "entrance-prose ratchet (every function, method, and class under app/ and tests/): "
        "the docstring AND the comment block above the first statement share ONE budget of "
        f"at most {_PROSE_CHAR_CEILING} characters — one short sentence. Moving a docstring "
        "into a comment block at the top of the body does NOT satisfy this rule: the budget "
        "counts both, so the prose has to get shorter, not change syntax. The default is NO "
        "prose at all; it earns its place only by carrying what the code cannot say (an "
        "invariant, a gotcha, units, a non-obvious why). Cut it to one short sentence, or "
        "move the content to a file under docs/ and reference that file from the code. A "
        "comment sitting BESIDE or BELOW the first statement is unbudgeted — that is where "
        "a note about a specific statement belongs. Adding an entry to the "
        "_JUSTIFIED_EXCEPTIONS dict in tests/arch/test_docstring_length_ratchet.py, with a "
        "written reason, should be very rare, and both frozensets beside it may only SHRINK "
        "— never add to either:\n  " + "\n  ".join(offenders)
    )


# --- qualified-name walk ---------------------------------------------------


def _measure_children(
    node: ast.AST,
    rel_path: str,
    prefix: str,
    overload_lines: set[int],
    comment_chars_by_line: dict[int, int],
) -> list[SymbolEntranceProse]:
    measurements: list[SymbolEntranceProse] = []
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, _DEFINITION_NODES):
            measurements.extend(
                _measure_children(child, rel_path, prefix, overload_lines, comment_chars_by_line)
            )
            continue
        symbol = f"{prefix}{child.name}"
        # clean=True dedents, deliberately: a continuation line's leading whitespace is
        # not prose, so a method nested three levels deep is not billed for its indent.
        docstring = ast.get_docstring(child, clean=True)
        docstring_chars = 0 if docstring is None else len(docstring)
        comment_chars = _sum_entrance_comment_chars(child, comment_chars_by_line)
        # An @typing.overload stub is dropped here, BEFORE the identity check, the way
        # the complexity ratchet does it: the stubs and their implementation all resolve
        # to one symbol, and a stub's body is the trivial `...` — nothing to measure.
        # A symbol carrying no prose at all is never recorded, so a list entry naming it
        # reads as stale.
        if docstring_chars + comment_chars > 0 and child.lineno not in overload_lines:
            measurements.append(
                SymbolEntranceProse(
                    rel_path, child.lineno, symbol, docstring_chars, comment_chars
                )
            )
        measurements.extend(
            _measure_children(
                child, rel_path, f"{symbol}.", overload_lines, comment_chars_by_line
            )
        )
    return measurements


def read_comment_chars_by_line(source: str) -> dict[int, int]:
    chars_by_line: dict[int, int] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            chars_by_line[token.start[0]] = len(_strip_comment_marker(token.string))
    return chars_by_line


def _strip_comment_marker(raw: str) -> str:
    """The comment as a reader meets it: without its ``#`` and the single space after it."""
    body = raw[1:]
    return body[1:] if body.startswith(" ") else body


def _sum_entrance_comment_chars(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    comment_chars_by_line: dict[int, int],
) -> int:
    docstring_node = node.body[0] if ast.get_docstring(node) is not None else None
    remaining = node.body[1:] if docstring_node is not None else node.body
    if not remaining:
        return 0
    lower = node.lineno if docstring_node is None else _end_line_of(docstring_node)
    upper = _first_line_of(remaining[0])
    return sum(chars for line, chars in comment_chars_by_line.items() if lower < line < upper)


def _end_line_of(statement: ast.stmt) -> int:
    if statement.end_lineno is None:
        raise ValueError(
            "entrance-prose ratchet: a docstring statement at line "
            f"{statement.lineno} carries no end_lineno, so the entrance window cannot be "
            "bounded — refusing to guess it"
        )
    return statement.end_lineno


def _first_line_of(statement: ast.stmt) -> int:
    # A decorated def opens at its `@`: a comment above that is not the parent's prose.
    if isinstance(statement, _DEFINITION_NODES) and statement.decorator_list:
        return min(decorator.lineno for decorator in statement.decorator_list)
    return statement.lineno


# --- offender messages -----------------------------------------------------


def _is_stale(key: str, by_symbol: dict[str, SymbolEntranceProse]) -> bool:
    return key not in by_symbol or by_symbol[key].prose_chars <= _PROSE_CHAR_CEILING


def _describe_violation(measurement: SymbolEntranceProse) -> str:
    return (
        f"{measurement.path}::{measurement.symbol}  prose_chars={measurement.prose_chars} "
        f"(docstring {measurement.docstring_chars} + entrance comments "
        f"{measurement.comment_chars} > {_PROSE_CHAR_CEILING}, not listed) — cut it to one "
        "short sentence, or move the content to docs/ and reference that file; moving it "
        "between the two syntaxes buys nothing, they share the budget"
    )


def _describe_stale_entry(key: str, list_name: str) -> str:
    return (
        f"{key}  (no longer over-ceiling entrance prose — the symbol was deleted or renamed, "
        f"or its docstring and entrance comments now total at or under {_PROSE_CHAR_CEILING} "
        f"chars) — delete the stale {list_name} entry"
    )


# --- unit tests for the checker, on tmp_path fixtures (red + green) -------


def _write_module(tmp_path: Path, body: str, name: str = "m.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _measurement(
    symbol: str = "go",
    docstring_chars: int = 150,
    comment_chars: int = 0,
    path: str = "app/m.py",
    line: int = 1,
) -> SymbolEntranceProse:
    return SymbolEntranceProse(
        path=path,
        line=line,
        symbol=symbol,
        docstring_chars=docstring_chars,
        comment_chars=comment_chars,
    )


def test_measure_symbol_entrance_prose_flags_a_function_over_the_ceiling(tmp_path: Path) -> None:
    file = _write_module(tmp_path, f'def go():\n    """{"x" * 101}"""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 101
    offenders = find_ratchet_violations([measurement], frozenset(), frozenset(), {})
    assert len(offenders) == 1
    assert "m.py::go" in offenders[0] and "one short sentence" in offenders[0]


def test_measure_symbol_entrance_prose_passes_a_docstring_of_exactly_the_ceiling(tmp_path: Path) -> None:
    file = _write_module(tmp_path, f'def go():\n    """{"x" * 100}"""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == _PROSE_CHAR_CEILING
    assert find_ratchet_violations([measurement], frozenset(), frozenset(), {}) == []


def test_measure_symbol_entrance_prose_skips_a_symbol_with_no_docstring(tmp_path: Path) -> None:
    file = _write_module(tmp_path, "def go():\n    return 1\n\n\nclass Foo:\n    x = 1\n")
    assert measure_symbol_entrance_prose([file], tmp_path) == []


def test_measure_symbol_entrance_prose_measures_a_class_docstring(tmp_path: Path) -> None:
    file = _write_module(tmp_path, 'class Foo:\n    """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "Foo"
    assert measurement.prose_chars == len("Short.")


def test_measure_symbol_entrance_prose_measures_an_async_function(tmp_path: Path) -> None:
    file = _write_module(tmp_path, 'async def go():\n    """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "go"


def test_measure_symbol_entrance_prose_ignores_the_module_docstring(tmp_path: Path) -> None:
    # The module ceiling is a separate rule (lines, not chars) with no owning symbol to name.
    file = _write_module(tmp_path, f'"""{"x" * 300}"""\ndef go():\n    """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "go"


def test_measure_symbol_entrance_prose_qualifies_a_method_as_class_dot_method(tmp_path: Path) -> None:
    file = _write_module(tmp_path, 'class Foo:\n    def bar(self):\n        """Short."""\n')
    symbols = {m.symbol for m in measure_symbol_entrance_prose([file], tmp_path)}
    assert symbols == {"Foo.bar"}


def test_measure_symbol_entrance_prose_qualifies_a_nested_class_and_closure(tmp_path: Path) -> None:
    file = _write_module(
        tmp_path,
        'class Outer:\n'
        '    """Short."""\n'
        '    class Inner:\n'
        '        """Short."""\n'
        '        def go(self):\n'
        '            """Short."""\n'
        '            def deeper():\n'
        '                """Short."""\n',
    )
    symbols = {m.symbol for m in measure_symbol_entrance_prose([file], tmp_path)}
    assert symbols == {"Outer", "Outer.Inner", "Outer.Inner.go", "Outer.Inner.go.deeper"}


def test_measure_symbol_entrance_prose_finds_a_def_nested_in_a_conditional(tmp_path: Path) -> None:
    file = _write_module(tmp_path, 'if True:\n    def go():\n        """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "go"


def test_measure_symbol_entrance_prose_dedents_a_deeply_nested_docstring(tmp_path: Path) -> None:
    file = _write_module(
        tmp_path,
        "class Foo:\n"
        "    class Bar:\n"
        "        def go(self):\n"
        '            """First.\n'
        "\n"
        '            Second."""\n',
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == len("First.\n\nSecond.")


def test_measure_symbol_entrance_prose_reads_relative_posix_path(tmp_path: Path) -> None:
    nested = tmp_path / "app" / "sub"
    nested.mkdir(parents=True)
    file = _write_module(nested, 'def go():\n    """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.path == "app/sub/m.py"


def test_measure_symbol_entrance_prose_excludes_overload_stubs_but_keeps_the_implementation(
    tmp_path: Path,
) -> None:
    # Without the exclusion an overload group's stubs and its body collide as one symbol.
    file = _write_module(
        tmp_path,
        "from typing import overload\n"
        "@overload\n"
        'def go(x: int) -> int:\n    """Short."""\n'
        "@overload\n"
        'def go(x: str) -> str:\n    """Short."""\n'
        'def go(x):\n    """Real."""\n',
    )
    measurements = measure_symbol_entrance_prose([file], tmp_path)
    assert [(m.symbol, m.line) for m in measurements] == [("go", 8)]


def test_index_by_symbol_raises_on_a_property_setter_pair(tmp_path: Path) -> None:
    file = _write_module(
        tmp_path,
        "class Foo:\n"
        "    @property\n"
        '    def x(self):\n        """Short."""\n'
        "    @x.setter\n"
        '    def x(self, value):\n        """Short."""\n',
    )
    measurements = measure_symbol_entrance_prose([file], tmp_path)
    with pytest.raises(ValueError, match="m.py::Foo.x") as excinfo:
        index_by_symbol(measurements)
    assert "lines 3 and 6" in str(excinfo.value)


def test_index_by_symbol_keeps_two_different_symbols_in_the_same_file() -> None:
    indexed = index_by_symbol([_measurement(symbol="go"), _measurement(symbol="stop")])
    assert set(indexed) == {"app/m.py::go", "app/m.py::stop"}


def test_find_ratchet_violations_reports_every_offender_sorted_by_symbol() -> None:
    offenders = find_ratchet_violations(
        [_measurement(symbol="zeta"), _measurement(symbol="alpha")], frozenset(), frozenset(), {}
    )
    assert [offender.split()[0] for offender in offenders] == [
        "app/m.py::alpha",
        "app/m.py::zeta",
    ]


def test_find_ratchet_violations_passes_a_grandfathered_symbol_over_the_ceiling() -> None:
    assert find_ratchet_violations([_measurement()], frozenset({"app/m.py::go"}), frozenset(), {}) == []


def test_find_ratchet_violations_passes_a_justified_exception_over_the_ceiling() -> None:
    assert find_ratchet_violations([_measurement()], frozenset(), frozenset(), {"app/m.py::go": "why"}) == []


def test_find_ratchet_violations_still_flags_an_unlisted_symbol_beside_a_listed_one() -> None:
    measurements = [_measurement(symbol="listed"), _measurement(symbol="unlisted")]
    offenders = find_ratchet_violations(measurements, frozenset({"app/m.py::listed"}), frozenset(), {})
    assert len(offenders) == 1
    assert "app/m.py::unlisted" in offenders[0]


def test_find_ratchet_violations_flags_a_grandfathered_entry_now_under_the_ceiling() -> None:
    cut = _measurement(docstring_chars=_PROSE_CHAR_CEILING)
    offenders = find_ratchet_violations([cut], frozenset({"app/m.py::go"}), frozenset(), {})
    assert len(offenders) == 1
    assert "delete the stale _GRANDFATHERED entry" in offenders[0]


def test_find_ratchet_violations_flags_a_grandfathered_entry_for_a_missing_symbol() -> None:
    offenders = find_ratchet_violations([], frozenset({"app/gone.py::go"}), frozenset(), {})
    assert len(offenders) == 1
    assert "app/gone.py::go" in offenders[0] and "_GRANDFATHERED" in offenders[0]


def test_find_ratchet_violations_flags_a_stale_justified_exception() -> None:
    cut = _measurement(docstring_chars=_PROSE_CHAR_CEILING)
    offenders = find_ratchet_violations([cut], frozenset(), frozenset(), {"app/m.py::go": "why"})
    assert len(offenders) == 1
    assert "delete the stale _JUSTIFIED_EXCEPTIONS entry" in offenders[0]


def test_find_ratchet_violations_raises_on_two_symbols_with_the_same_identity() -> None:
    duplicates = [_measurement(line=1), _measurement(line=10, docstring_chars=5)]
    with pytest.raises(ValueError, match="app/m.py::go"):
        find_ratchet_violations(duplicates, frozenset(), frozenset(), {})


def test_justified_exceptions_ships_empty_and_every_entry_carries_a_reason() -> None:
    assert _JUSTIFIED_EXCEPTIONS == {}
    assert all(reason.strip() for reason in _JUSTIFIED_EXCEPTIONS.values())


def test_no_list_exempts_this_rules_own_file() -> None:
    own_file = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    listed = sorted(
        key
        for key in (
            set(_GRANDFATHERED) | set(_GRANDFATHERED_ENTRANCE_PROSE) | set(_JUSTIFIED_EXCEPTIONS)
        )
        if key.split("::", 1)[0] == own_file
    )
    assert not listed, (
        "a rule must never exempt itself: these entries let this file's own entrance prose "
        f"past the {_PROSE_CHAR_CEILING}-character ceiling it enforces on everyone "
        "else. Both frozensets are only for symbols that PREDATE the measure they name, and "
        "this file was written under it — so delete these entries and cut the prose (or drop "
        "it: a well-named checker test rarely needs any):\n  " + "\n  ".join(listed)
    )


def test_grandfathered_entries_are_symbol_keys_not_line_numbers() -> None:
    listed = _GRANDFATHERED | _GRANDFATHERED_ENTRANCE_PROSE
    assert all("::" in key and not key.rsplit("::", 1)[1].isdigit() for key in listed)


def test_the_two_grandfather_lists_do_not_overlap() -> None:
    assert not (_GRANDFATHERED & _GRANDFATHERED_ENTRANCE_PROSE)


def test_the_rule_governs_a_non_empty_set_of_files() -> None:
    governed = {
        path.relative_to(_REPO_ROOT).as_posix()
        for path in find_governed_files(_APP_ROOT, _TESTS_ROOT)
    }
    assert "app/main.py" in governed
    assert "tests/arch/test_docstring_length_ratchet.py" in governed


def test_find_governed_files_raises_when_a_root_has_no_python_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="governs no source files"):
        find_governed_files(tmp_path, tmp_path)


# --- the entrance comment block counts against the same budget -------------


def _comment_block(chars: int, indent: str = "    ") -> str:
    return f"{indent}# {'x' * chars}"


def test_measure_symbol_entrance_prose_flags_an_undocstringed_function_whose_entrance_comment_is_long(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "def go():\n" + _comment_block(55) + "\n" + _comment_block(55) + "\n    return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 110
    assert len(find_ratchet_violations([measurement], frozenset(), frozenset(), {})) == 1


def test_measure_symbol_entrance_prose_adds_the_entrance_comment_to_a_docstring_under_the_ceiling(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        f'def go():\n    """{"d" * 60}"""\n' + _comment_block(60) + "\n    return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 120
    assert len(find_ratchet_violations([measurement], frozenset(), frozenset(), {})) == 1


def test_measure_symbol_entrance_prose_ignores_a_comment_below_the_first_statement(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        f'def go():\n    """{"d" * 60}"""\n    total = 1\n'
        + _comment_block(60)
        + "\n    return total\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 60
    assert find_ratchet_violations([measurement], frozenset(), frozenset(), {}) == []


def test_measure_symbol_entrance_prose_counts_a_comment_above_the_first_field_of_a_class(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "class Foo:\n" + _comment_block(55) + "\n" + _comment_block(55) + "\n    x: int = 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "Foo"
    assert measurement.prose_chars == 110


def test_measure_symbol_entrance_prose_counts_an_entrance_comment_split_off_by_a_blank_line(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "def go():\n" + _comment_block(55) + "\n" + _comment_block(55) + "\n\n    return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 110


def test_measure_symbol_entrance_prose_records_nothing_for_a_symbol_with_neither_prose_form(
    tmp_path: Path,
) -> None:
    file = _write_module(tmp_path, "def go():\n    return 1  # trailing note, not the entrance\n")
    assert measure_symbol_entrance_prose([file], tmp_path) == []


def test_measure_symbol_entrance_prose_counts_a_comment_inside_a_multi_line_signature(
    tmp_path: Path,
) -> None:
    # The window opens at the `def` line, so a note among the parameters is entrance prose.
    file = _write_module(
        tmp_path,
        "def go(\n" + _comment_block(55) + "\n" + _comment_block(55) + "\n):\n    return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 110


def test_measure_symbol_entrance_prose_closes_the_window_at_a_decorated_first_statement(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "class Foo:\n"
        + _comment_block(55)
        + "\n    @property\n"
        + _comment_block(70)
        + "\n    def bar(self):\n        return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert (measurement.symbol, measurement.prose_chars) == ("Foo", 55)


def test_read_comment_chars_by_line_bills_the_prose_a_reader_sees_not_the_marker() -> None:
    chars_by_line = read_comment_chars_by_line("# abc\n#abc\n## abc\nx = 1  # abc\n")
    assert chars_by_line == {1: 3, 2: 3, 3: 5, 4: 3}


def test_find_ratchet_violations_flags_a_stale_entrance_prose_entry() -> None:
    cut = _measurement(docstring_chars=_PROSE_CHAR_CEILING)
    offenders = find_ratchet_violations([cut], frozenset(), frozenset({"app/m.py::go"}), {})
    assert len(offenders) == 1
    assert "delete the stale _GRANDFATHERED_ENTRANCE_PROSE entry" in offenders[0]


def test_measure_symbol_entrance_prose_flags_the_pending_review_shape_that_motivated_the_rule(
    tmp_path: Path,
) -> None:
    # `PendingReview`'s shape: a docstring cut to fit, its paragraph moved above the fields.
    file = _write_module(
        tmp_path,
        "class PendingReview:\n"
        '    """One row awaiting a human decision, carried on the deferred marker of the'
        ' row that made it."""\n'
        "\n"
        "    # The key the cache was searched under, and a copy of the row exactly as it\n"
        "    # arrived from upstream.\n"
        "    input_fingerprint: str\n"
        "    frozen_row: dict[str, str]\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars > _PROSE_CHAR_CEILING
    assert len(find_ratchet_violations([measurement], frozenset(), frozenset(), {})) == 1
