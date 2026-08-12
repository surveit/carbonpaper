"""Turning the file store's numbers into what a person reads: sizes, and the sentence a
refusal is. The service raises with the facts; the wording lives here."""
from __future__ import annotations

from app.services.errors import FileOverCeiling, StoreOverQuota
from app.services.uploads import UploadedFile

_KILOBYTE = 1024
_MEGABYTE = 1024 * _KILOBYTE
_GIGABYTE = 1024 * _MEGABYTE


def describe_bytes(count: int) -> str:
    """A size for a person reading a refusal, so 512MB is not shown as 536870912."""
    if count >= _GIGABYTE:
        return f"{count / _GIGABYTE:.3g}GB"
    if count >= _MEGABYTE:
        return f"{count / _MEGABYTE:.3g}MB"
    if count >= _KILOBYTE:
        return f"{count / _KILOBYTE:.3g}KB"
    return f"{count}B"


def describe_refusal(exc: FileOverCeiling | StoreOverQuota) -> str:
    """Why the file was not kept, and what to do about it."""
    if isinstance(exc, FileOverCeiling):
        return (f"this file is over the {describe_bytes(exc.ceiling)} limit for a single "
                "input. That ceiling is what a run on this machine can load into memory, "
                "so a larger file would upload and then fail every run that read it. Cut "
                "the file down, or convert it to parquet.")
    return (f"stored files would reach {describe_bytes(exc.used)}, over the "
            f"{describe_bytes(exc.quota)} limit — the {describe_bytes(exc.sent)} just "
            "sent was not kept. Every file before it was, and nothing in the app deletes "
            f"one: clear {exc.root} on the server, or raise "
            "CARBON_PAPER_FILES_QUOTA_BYTES.")


def describe_attachment(record: UploadedFile, project_name: str = "") -> str:
    """The one sentence a chat shows for an attached file AND sends to the agent."""
    home = ("not in a project yet" if not record.project_id
            else f"in project {project_name or record.project_id} ({record.project_id})")
    # One sentence for both: the agent reads the turn's text and never the page, so a
    # card saying one thing while the model is told another is two records of one
    # event. sha256 is in it because that is what run_workflow's `files` takes.
    return (f"[file] {record.filename} · {describe_bytes(record.byte_count)} · "
            f"{home} · sha256 {record.sha256}")
