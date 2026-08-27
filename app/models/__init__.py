"""The workflow contract, as Pydantic models.

Per-stage-type config models live in `app.models.stages.<type>`; import them from there.
"""
from app.models.compiler_warnings import (
    CompilerWarningReport as CompilerWarningReport,
    find_stage_compiler_warnings as find_stage_compiler_warnings,
    find_workflow_compiler_warnings as find_workflow_compiler_warnings,
)
from app.models.errors import StepRefused as StepRefused
from app.models.schema import (
    Column as Column,
    DATE_COLUMN_TYPES as DATE_COLUMN_TYPES,
    FunctionKind as FunctionKind,
    JSON_COLUMN_TYPE as JSON_COLUMN_TYPE,
    LIST_JSON_COLUMN_TYPE as LIST_JSON_COLUMN_TYPE,
    RANGE_UNBOUNDED_MARKER as RANGE_UNBOUNDED_MARKER,
    STR_COLUMN_TYPE as STR_COLUMN_TYPE,
    SourceRef as SourceRef,
    TableSchema as TableSchema,
    is_valid_column_type as is_valid_column_type,
)
from app.models.stage import (
    ReviewConfig as ReviewConfig,
    Stage as Stage,
    AbstractStage as AbstractStage,
    StageDraft as StageDraft,
    StageEdit as StageEdit,
    StageInput as StageInput,
    StageType as StageType,
    STAGE_SPEC_SCHEMA_VERSION as STAGE_SPEC_SCHEMA_VERSION,
    parse_stage as parse_stage,
    stage_to_json as stage_to_json,
    stage_to_spec_dict as stage_to_spec_dict,
    validate_stage as validate_stage,
)
from app.models.workflow import (
    Workflow as Workflow,
    WorkflowNotFormed as WorkflowNotFormed,
    build_workflow as build_workflow,
    detect_cycle as detect_cycle,
    find_stages_reaching_report as find_stages_reaching_report,
    parse_workflow as parse_workflow,
    resolve_workflow_stages as resolve_workflow_stages,
    validate_inputs_resolve as validate_inputs_resolve,
    validate_report_is_terminal as validate_report_is_terminal,
    validate_unique_ids as validate_unique_ids,
    validate_workflow as validate_workflow,
    validate_workflow_draft as validate_workflow_draft,
)
from app.models.named_schemas import (
    NamedColumn as NamedColumn,
    NamedSchema as NamedSchema,
    SchemaKind as SchemaKind,
    SchemaLibrary as SchemaLibrary,
    parse_reference as parse_reference,
    parse_schema_library as parse_schema_library,
    validate_named_schema as validate_named_schema,
    validate_references_resolve as validate_references_resolve,
    validate_schema_library as validate_schema_library,
    validate_unique_schema_names as validate_unique_schema_names,
)
from app.models.terms import (
    Terms as Terms,
    Verb as Verb,
    render_terms as render_terms,
    validate_one_meaning_per_word as validate_one_meaning_per_word,
)
from app.models.workflow_stage import (
    WorkflowStage as WorkflowStage,
    WorkflowStageInput as WorkflowStageInput,
)
from app.models.table import TableRef as TableRef
from app.models.eval import (
    CodeScorer as CodeScorer,
    EvalRunSettings as EvalRunSettings,
    ExpectedOutput as ExpectedOutput,
    ScoringMetric as ScoringMetric,
    StageOutputOverride as StageOutputOverride,
)
# NOTE: the compiled-stage loader lives in app.services.loader (it does
# filesystem I/O, which is service work, not schema). Import it from there;
# app.models stays a pure, side-effect-free schema package.

# Scalar column-type vocabulary, re-exported from schema.py (its single
# definition). `list[<type>]` / dict / json are handled by is_valid_column_type.
from app.models.schema import SCALAR_COLUMN_TYPES as SCALAR_COLUMN_TYPES

