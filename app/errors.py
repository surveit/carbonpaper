"""Domain exceptions, declared here rather than inline in the modules that
raise them, so failure types have one home and can be imported without pulling
in a whole subsystem.

Keep this module dependency-free (standard library only): errors are imported
widely, including by low-level modules, so importing app packages here would
risk import cycles."""
from __future__ import annotations


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
