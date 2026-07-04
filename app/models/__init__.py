"""The methodology DAG contract, as Pydantic models.

Split across modules:
  - schema.py        — model base + the Column/TableSchema primitives
  - stage.py         — node types, handle blocks, the Stage model
  - methodology.py   — the Methodology (DAG) model + cross-stage graph checks
  - named_schemas.py — the named data model (NamedSchema, SchemaLibrary)
  - table.py         — TableRef (a general on-disk table pointer)
  - eval.py          — the eval contract (EvalConfig, EvalRun, scorability)

Import from `app.models` (this aggregator) for the stable public surface.
"""
from app.models.schema import (
    Column,
    SourceRef,
    TableSchema,
    is_valid_column_type,
)
from app.models.stage import (
    AggFormula,
    AggregateConfig,
    AggregationOp,
    Connector,
    ConnectorKind,
    FileFormat,
    FunctionKind,
    InputRef,
    JoinConfig,
    JoinKey,
    JoinType,
    LLMConfig,
    PublishConfig,
    PublishFormat,
    PythonFunction,
    QueueConfig,
    ReviewConfig,
    Stage,
    StageType,
    validate_stage,
)
from app.models.methodology import (
    Methodology,
    check_inputs_resolve,
    check_unique_ids,
    detect_cycle,
    parse_methodology,
    validate_methodology,
    validate_methodology_stages,
)
from app.models.named_schemas import (
    NamedColumn,
    NamedSchema,
    SchemaKind,
    SchemaLibrary,
    check_references_resolve,
    check_unique_schema_names,
    parse_reference,
    parse_schema_library,
    validate_schema_library,
)
from app.models.table import TableRef
from app.models.eval import (
    CodeScorer,
    EvalConfig,
    EvalRun,
    EvalRunSettings,
    ExpectedColumn,
    StageOutputOverride,
    resolve_eval_run_settings,
)
# NOTE: the compiled-stage loader lives in app.services.loader (it does
# filesystem I/O, which is service work, not schema). Import it from there;
# app.models stays a pure, side-effect-free schema package.

__all__ = [
    "StageType", "ConnectorKind", "FileFormat", "AggFormula", "JoinType",
    "FunctionKind", "PublishFormat", "is_valid_column_type",
    "SourceRef", "Column", "TableSchema", "Connector", "LLMConfig",
    "PythonFunction", "JoinKey", "JoinConfig", "AggregationOp",
    "AggregateConfig", "QueueConfig", "PublishConfig", "ReviewConfig",
    "InputRef", "Stage", "validate_stage",
    "Methodology", "parse_methodology", "validate_methodology",
    "validate_methodology_stages",
    "check_unique_ids", "check_inputs_resolve", "detect_cycle",
    "SchemaKind", "NamedColumn", "NamedSchema", "SchemaLibrary",
    "parse_schema_library", "validate_schema_library",
    "check_unique_schema_names", "check_references_resolve", "parse_reference",
    # general
    "TableRef",
    # eval contract
    "StageOutputOverride", "ExpectedColumn", "CodeScorer", "EvalConfig",
    "EvalRunSettings", "resolve_eval_run_settings", "EvalRun",
]
