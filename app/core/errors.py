# Dependency-free by rule (standard library only): errors are imported widely,
# including by low-level modules, so an app import here would risk a cycle.
from __future__ import annotations


class StageNotInRun(ValueError):
    pass


class StageOutputMissing(ValueError):
    pass


class RowOutOfRange(ValueError):
    pass


class CitationMismatch(ValueError):
    """A publish stage cited a cell for a value that cell does not hold."""


class NoVersionToRunError(Exception):
    """No stored version at all; a named version_id with no document raises FileNotFoundError."""


class RunVersionUnresolvableError(Exception):
    pass


class GenerationError(Exception):
    pass


class EvalNotScorableError(Exception):
    pass


class EvalGrainViolationError(Exception):
    pass


class SubsetRunError(Exception):
    pass


class TraceLinksUnavailableError(Exception):
    pass


class NoWorkflowTestSourceError(Exception):
    pass


class NoWorkflowTestVersionError(Exception):
    pass


class LLMError(Exception):
    pass


class DocumentNotFound(Exception):
    """Raised by the strict read path; the tolerant read (`read_tolerant`/`load_or_none`) returns None."""


class PersistenceError(Exception):
    """The configured document store could not complete an operation."""


class FrameNotSerializableError(Exception):
    """A dtype/shape parquet cannot represent. A disk/OS error is NOT reported this way — it propagates."""


# Named rather than null-filled: a union's inputs are declared schema-identical,
# so a column present on one side and not the other is a bug upstream, not a
# shape for the concatenation to paper over with a value nothing supplied.
class FrameConcatMismatchError(ValueError):
    """Tables given to `concat_tables` disagree on their column names."""


class ProjectExistsError(Exception):
    pass


class DraftNotFoundError(Exception):
    pass


class MissingInputBindingError(Exception):
    pass


class RunNotFoundError(Exception):
    pass


class RunManifestNotJson(ValueError):
    pass


class ReviewValidationError(ValueError):
    pass


class ReviewGuideValidationError(ValueError):
    """Raised on WRITE, so an invalid guide is never stored."""


class PredicateError(ValueError):
    pass


class NoRowsToSelectFrom(Exception):
    """No finished run holds the rows a stage's examples would be selected from."""
