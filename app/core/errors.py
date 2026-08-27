# Dependency-free by rule (standard library only): errors are imported widely,
# including by low-level modules, so an app import here would risk a cycle.
from __future__ import annotations

from pathlib import Path


class StageNotInRun(ValueError):
    pass


class StageOutputMissing(ValueError):
    pass


class RowOutOfRange(ValueError):
    pass


class ContributorNotInFanIn(ValueError):
    """A trace was told to follow a contributor the run's lineage does not record."""


class CitationMismatch(ValueError):
    """A report stage cited a cell for a value that cell does not hold."""


class ColumnNotInFrame(ValueError):
    pass


class CellIsNotAScalar(ValueError):
    """A frame cell holds a list, struct or other payload no scalar reader can carry."""


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


class StageWideFailure(Exception):
    """A failure whose scope is the STAGE, so trying another row cannot help."""
    # The row driver abandons its fan-out on one instead of tagging every
    # remaining row with the same message. Subclassed, never raised directly.


class DocumentNotFound(Exception):
    """Raised by the strict read path; the tolerant read (`read_tolerant`/`load_or_none`) returns None."""


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


class FileNotStoredError(Exception):
    """A file id the project has no stored bytes for."""


class FileOverCeiling(Exception):
    """Carries the numbers, not a sentence — a surface writes the sentence."""

    def __init__(self, *, ceiling: int) -> None:
        self.ceiling = ceiling
        super().__init__(f"file over the {ceiling}-byte ceiling")


class StoreOverQuota(Exception):
    """Carries the numbers, not a sentence — a surface writes the sentence."""

    def __init__(self, *, used: int, quota: int, sent: int, root: Path) -> None:
        self.used, self.quota, self.sent, self.root = used, quota, sent, root
        super().__init__(f"store would reach {used} bytes, over the {quota}-byte limit")
