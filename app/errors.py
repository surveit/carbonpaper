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


class NoVersionToRunError(Exception):
    """A run was requested for a project that has no version to run.

    Runs are read-only with respect to versions: a run targets an existing
    version and never creates one. Version creation is an explicit act (the
    "Create version" action). Raised when `version_id` is None and no version
    exists yet — rather than fabricating a snapshot as a run side effect, which
    would immortalise (and potentially poison) the working copy."""


class RegenerateWithoutSnapshotError(Exception):
    """Raised when a from-scratch compile would overwrite reviewed work without a
    prior version snapshot and without explicit confirm_overwrite."""


class GenerationError(Exception):
    """A headless agent generation could not produce a VALID artifact.

    Raised by `app.agent.agent.Agent.run` when the agent does not submit output that
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
