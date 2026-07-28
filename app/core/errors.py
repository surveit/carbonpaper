"""Domain exceptions, declared here rather than inline in the modules that
raise them, so failure types have one home and can be imported without pulling
in a whole subsystem.

Keep this module dependency-free (standard library only): errors are imported
widely, including by low-level modules, so importing app packages here would
risk import cycles."""
from __future__ import annotations


class StageNotInRun(ValueError):
    """A trace was requested for a stage id absent from the run's manifest — a
    bad path/param (→ 404), not an internal fault."""


class RowOutOfRange(ValueError):
    """A trace was requested for a row ordinal outside a stage's output — a bad
    path/param (→ 400), not an internal fault."""


class TraceUnavailableError(Exception):
    """Raised instead of returning an href to a trace page that was not written."""


class TraceRowNotStamped(Exception):
    """A trace was requested for a row lacking the ordinal the runtime stamps on."""


class TraceOrdinalColumnCollision(ValueError):
    """A publish input already holds the column name the runtime stamps ordinals into."""


class NoVersionToRunError(Exception):
    """A run was requested for a project that has no PUBLISHED version to run.

    Runs are read-only with respect to versions: a run targets an existing,
    published version and never creates or publishes one. Version creation and
    publishing are separate explicit acts (the "Create version" and "Publish"
    actions). Raised when `version_id` is None and no version is published yet
    — rather than fabricating a snapshot as a run side effect, which would
    immortalise (and potentially poison) the working copy — and when an
    explicit `version_id` names a version that exists but isn't published."""


class RunVersionUnresolvableError(Exception):
    """A run's manifest names no `workflow_version`, or names one whose version
    document is missing or no longer validates, so what the run executed cannot
    be read. Its message is shown to the reader in place of the graph."""


class GenerationError(Exception):
    """A headless agent generation could not produce a VALID artifact.

    Raised by `app.core.agent.agent.Agent.run` when the agent does not submit output that
    validates against the target schema within its attempt budget. Fails loudly rather
    than returning or persisting a partial or fabricated result — the caller logs the
    failure honestly, never a fake success."""


class EvalNotScorableError(Exception):
    """An eval run was requested but the config can't be scored as it stands:
    incompatible with the workflow, has no eval dataset, or taps a path that
    isn't grain-preserving and carries no code scorer. The reason is the message."""


class EvalGrainViolationError(Exception):
    """The pathway that compatibility judged grain-preserving did not, in fact,
    return one target row per injected eval-dataset row — so the rows can't be
    aligned by position to score. Raised (loudly) rather than aligning a
    mismatched pair and reporting a fabricated result."""


class SubsetRunError(Exception):
    """Running a subset of a workflow did not cleanly produce every requested
    stage output: a stage errored, or the run halted for human review. The
    message names what went wrong. (General runtime failure — callers like the
    eval runner translate it into their own outcome, e.g. an `error` eval run.)"""


class NoWorkflowTestSourceError(Exception):
    """A workflow test was requested on a workflow with no input_data stage to
    sample from — there is no bound source to slice a preview off, so nothing can
    be seeded and the frontier cannot run. Raised (loudly) rather than
    workflow-testing an empty injection that every downstream stage would then
    error on."""


class NoWorkflowTestVersionError(Exception):
    """A workflow test was requested on a project with no stored workflow version
    to sample against. Unlike a production run (which pins a PUBLISHED version), a
    workflow test accepts any stored immutable version — but there must be at least
    one. Raised (loudly, naming the project) rather than falling back to the
    working copy or fabricating a version."""


class LLMError(Exception):
    """A live-LLM call failed, or no LLM backend is available."""


class DocumentNotFound(Exception):
    """No document exists for a (collection, id) in the store. Raised by the
    strict read path — `SqliteKvStore.read`/`.schema_version` and
    `PersistedModel.load`. The tolerant path (`read_tolerant` /
    `PersistedModel.load_or_none`) returns None instead. A genuine miss
    surfaced loudly, never a fabricated empty document."""


class ProjectExistsError(Exception):
    """A project create was requested for a name whose examples/<name>/ directory
    already exists. Raised (loudly) rather than clobbering existing data — the
    rename is the human's decision."""


class DraftNotFoundError(Exception):
    """No draft exists for a (project, draft_id) — the id is malformed (fails the
    word-triplet shape), or well-formed but no such document is stored. Drafts
    are disposable scratch space with no promise of survival, so a miss is an
    ordinary outcome: the caller starts a new one with create_draft rather than
    treating this as corruption."""


class MissingInputBindingError(Exception):
    """A run was requested but at least one stage's preflight found it unready
    to run — e.g. an input stage with no file bound (no run binding supplied
    and the workflow itself authors no path), or bound to a file that does not
    exist. Raised before the run directory is created — a run never starts on
    inputs that would have to be guessed. The message names every unready
    stage."""


class RunNotFoundError(Exception):
    """No run exists for a (project, run_id): the run directory has no
    manifest.json — a bad/expired run id, not an internal fault. Raised (loudly)
    by the run service's status read rather than returning an empty or fabricated
    manifest for a run that never happened."""


class ReviewValidationError(ValueError):
    """A submitted review verdict is invalid (unknown verdict, or `modify`
    without a numeric score)."""


class PredicateError(ValueError):
    """A `where`/`filter` expression (aggregate.where, human_review_queue.filter)
    falls outside the closed grammar `app.core.predicate.parse_predicate`
    accepts — unparseable as Python, or built from a construct the grammar
    does not admit (a bare function call, arithmetic, subscripting, and the
    like). Raised at parse time, before either save-time column validation or
    runtime evaluation acts on the expression, so a rejected filter never
    reaches `pandas.eval`/`.query()` unchecked."""
