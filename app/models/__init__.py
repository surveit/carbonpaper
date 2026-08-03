"""The workflow contract, as Pydantic models.

Import from `app.models` (this aggregator) for the stable public surface.
"""
from app.models.compiler_warnings import (
    CompilerWarningReport as CompilerWarningReport,
    find_stage_compiler_warnings as find_stage_compiler_warnings,
    find_workflow_compiler_warnings as find_workflow_compiler_warnings,
)
from app.models.stages.warnings import CompilerWarning as CompilerWarning
from app.models.coverage import Coverage as Coverage
from app.models.errors import StepRefused as StepRefused
from app.models.node_contract_notes import (
    CODE_SUMMARY_CONTRACT_NOTE as CODE_SUMMARY_CONTRACT_NOTE,
    HUMAN_REVIEW_QUEUE_CONTRACT_NOTE as HUMAN_REVIEW_QUEUE_CONTRACT_NOTE,
)
from app.models.schema import (
    Column as Column,
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
    StageBase as StageBase,
    StageDraft as StageDraft,
    StageInput as StageInput,
    StageType as StageType,
    parse_stage as parse_stage,
    validate_stage as validate_stage,
)
from app.models.stages.aggregate import (
    AggFormula as AggFormula,
    AggregateConfig as AggregateConfig,
    AggregationOp as AggregationOp,
)
from app.models.stages.filter_rows import FilterConfig as FilterConfig
from app.models.stages.human_review_queue import (
    QueueConfig as QueueConfig,
    RowReviewDecision as RowReviewDecision,
)
from app.models.stages.input_data import (
    Connector as Connector,
    ConnectorKind as ConnectorKind,
    FileFormat as FileFormat,
    XlsxReadParams as XlsxReadParams,
)
from app.models.stages.join import (
    JoinConfig as JoinConfig,
    JoinKey as JoinKey,
)
from app.models.stages.llm_transform import LLMConfig as LLMConfig
from app.models.stages.publish import (
    PublishConfig as PublishConfig,
    PublishFormat as PublishFormat,
)
from app.models.stages.union import UnionConfig as UnionConfig
from app.models.stages.code import PythonFunction as PythonFunction
from app.models.stages.stage_tests import StageTest as StageTest
from app.models.workflow import (
    Workflow as Workflow,
    detect_cycle as detect_cycle,
    parse_workflow as parse_workflow,
    validate_edge_schemas as validate_edge_schemas,
    validate_inputs_resolve as validate_inputs_resolve,
    validate_publish_is_terminal as validate_publish_is_terminal,
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
from app.models.table import TableRef as TableRef
from app.models.eval import (
    CodeScorer as CodeScorer,
    EvalConfig as EvalConfig,
    EvalRun as EvalRun,
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

