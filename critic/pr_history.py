from __future__ import annotations

from datetime import datetime

from pydantic import Field

from critic.gh import GitHubCliError, read_api_list, read_graphql
from critic.records import CriticRecord, ForeignRecord, UncheckedGitHubJson

BODY_EDITS_LIMIT = 100
BASE_CHANGED_EVENT = "base_ref_changed"
RENAMED_EVENT = "renamed"
_BODY_EDITS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      userContentEdits(first: 100) { totalCount nodes { editedAt deletedAt diff } }
    }
  }
}
"""


class BodyVersion(CriticRecord):
    edited_at: str
    deleted_at: str | None
    body: str | None


class Rename(CriticRecord):
    renamed_at: str
    old_title: str
    new_title: str


class PrHistory(CriticRecord):
    """What changed on a PR after it opened, so a diff can be shown as the reviewer read it."""

    body_versions: list[BodyVersion]
    renames: list[Rename]
    base_changes: list[str]


class UnknownBodyError(RuntimeError):
    pass


class _RenameDetail(ForeignRecord):
    old_title: str = Field(alias="from")
    new_title: str = Field(alias="to")


class _IssueEvent(ForeignRecord):
    event: str
    created_at: str
    # Only a renamed event carries one.
    rename: _RenameDetail | None = None


class _Edit(ForeignRecord):
    editedAt: str
    deletedAt: str | None
    # GitHub names the field `diff`, but it holds the whole description as of that edit.
    diff: str | None


class _Edits(ForeignRecord):
    totalCount: int
    nodes: list[_Edit]


class _PullRequest(ForeignRecord):
    userContentEdits: _Edits


class _Repository(ForeignRecord):
    pullRequest: _PullRequest


class _Data(ForeignRecord):
    repository: _Repository


class _BodyEditsResponse(ForeignRecord):
    data: _Data


def fetch_pr_history(repo: str, number: int) -> PrHistory:
    events = read_api_list(f"repos/{repo}/issues/{number}/events?per_page=100")
    owner, name = repo.split("/", 1)
    edits = read_graphql(_BODY_EDITS_QUERY, {"owner": owner, "name": name, "number": number})
    return read_pr_history(events, edits)


def read_pr_history(events: list[UncheckedGitHubJson], edits: UncheckedGitHubJson) -> PrHistory:
    parsed = sorted((_IssueEvent.model_validate(item) for item in events), key=lambda e: e.created_at)
    return PrHistory(
        body_versions=read_body_versions(edits),
        renames=[_read_rename(event) for event in parsed if event.event == RENAMED_EVENT],
        base_changes=[event.created_at for event in parsed if event.event == BASE_CHANGED_EVENT],
    )


def read_body_versions(edits_response: UncheckedGitHubJson) -> list[BodyVersion]:
    response = _BodyEditsResponse.model_validate(edits_response)
    edits = response.data.repository.pullRequest.userContentEdits
    if edits.totalCount > BODY_EDITS_LIMIT:
        raise GitHubCliError(f"{edits.totalCount} description edits; only {BODY_EDITS_LIMIT} were read")
    versions = [
        BodyVersion(edited_at=edit.editedAt, deleted_at=edit.deletedAt, body=edit.diff)
        for edit in edits.nodes
    ]
    return sorted(versions, key=lambda version: version.edited_at)


def find_title_at(history: PrHistory, current_title: str, when: str) -> str:
    later = [rename for rename in history.renames if _parse(rename.renamed_at) > _parse(when)]
    return current_title if not later else later[0].old_title


def find_body_at(history: PrHistory, current_body: str | None, when: str) -> str | None:
    if not history.body_versions:
        return current_body
    earlier = [v for v in history.body_versions if _parse(v.edited_at) <= _parse(when)]
    if not earlier:
        raise UnknownBodyError(f"no description version predates {when}")
    if earlier[-1].deleted_at is not None:
        raise UnknownBodyError(f"the description in effect at {when} was deleted on GitHub")
    return earlier[-1].body


def find_base_changes_after(history: PrHistory, when: str) -> list[str]:
    return [change for change in history.base_changes if _parse(change) > _parse(when)]


def _read_rename(event: _IssueEvent) -> Rename:
    if event.rename is None:
        raise GitHubCliError(f"a renamed event at {event.created_at} carries no titles")
    return Rename(
        renamed_at=event.created_at,
        old_title=event.rename.old_title,
        new_title=event.rename.new_title,
    )


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)
