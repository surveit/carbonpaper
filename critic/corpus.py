from __future__ import annotations

import json
from pathlib import Path

from critic.gh import read_api_list, read_pr_list
from critic.records import CriticRecord, ForeignRecord, UncheckedGitHubJson

PR_FIELDS = (
    "number,title,body,author,createdAt,mergedAt,closedAt,state,url,additions,deletions,"
    "changedFiles,headRefName,baseRefName,reviewDecision,isDraft"
)
PR_LIST_LIMIT = 5000
PRS_FILE = "prs.json"
REVIEW_COMMENTS_FILE = "review_comments.json"
ISSUE_COMMENTS_FILE = "issue_comments.json"
REVIEWS_FILE = "reviews.jsonl"


class InlineComment(ForeignRecord):
    id: int
    html_url: str
    pull_request_url: str
    path: str
    body: str
    diff_hunk: str
    created_at: str
    original_commit_id: str
    original_line: int | None
    # GitHub omits these keys on a one-line comment and on a thread's first comment.
    original_start_line: int | None = None
    in_reply_to_id: int | None = None
    side: str | None = None

    @property
    def pr_number(self) -> int:
        return int(self.pull_request_url.rsplit("/", 1)[1])


class CorpusIndex(CriticRecord):
    by_url: dict[str, InlineComment]
    by_id: dict[int, InlineComment]


class CorpusCounts(CriticRecord):
    prs: int
    review_comments: int
    issue_comments: int
    reviews: int


class PullRequestListing(ForeignRecord):
    number: int
    additions: int
    deletions: int
    # None while the PR is open or after it closed unmerged.
    mergedAt: str | None

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions


def fetch_corpus(repo: str, out_dir: Path) -> CorpusCounts:
    out_dir.mkdir(parents=True, exist_ok=True)
    prs = read_pr_list(repo, PR_FIELDS, PR_LIST_LIMIT)
    _write_json(out_dir / PRS_FILE, prs)
    review_comments = read_api_list(_build_list_path(repo, "pulls/comments"))
    _write_json(out_dir / REVIEW_COMMENTS_FILE, review_comments)
    issue_comments = read_api_list(_build_list_path(repo, "issues/comments"))
    _write_json(out_dir / ISSUE_COMMENTS_FILE, issue_comments)
    numbers = [PullRequestListing.model_validate(pr).number for pr in prs]
    reviews = [review for number in numbers for review in fetch_reviews(repo, number)]
    _write_jsonl(out_dir / REVIEWS_FILE, reviews)
    return CorpusCounts(
        prs=len(prs),
        review_comments=len(review_comments),
        issue_comments=len(issue_comments),
        reviews=len(reviews),
    )


def fetch_reviews(repo: str, number: int) -> list[UncheckedGitHubJson]:
    reviews = read_api_list(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
    return [{**review, "pr": number} for review in reviews]


def load_corpus_index(corpus_dir: Path) -> CorpusIndex:
    comments = load_inline_comments(corpus_dir)
    return CorpusIndex(
        by_url={comment.html_url: comment for comment in comments},
        by_id={comment.id: comment for comment in comments},
    )


def load_pr_listings(corpus_dir: Path) -> list[PullRequestListing]:
    path = corpus_dir / PRS_FILE
    if not path.is_file():
        raise FileNotFoundError(f"no {PRS_FILE} in {corpus_dir}: run `fetch` first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [PullRequestListing.model_validate(item) for item in payload]


def load_inline_comments(corpus_dir: Path) -> list[InlineComment]:
    path = corpus_dir / REVIEW_COMMENTS_FILE
    if not path.is_file():
        raise FileNotFoundError(f"no {REVIEW_COMMENTS_FILE} in {corpus_dir}: run `fetch` first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [InlineComment.model_validate(item) for item in payload]


def _build_list_path(repo: str, collection: str) -> str:
    return f"repos/{repo}/{collection}?per_page=100&sort=created&direction=asc"


def _write_json(path: Path, payload: list[UncheckedGitHubJson]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[UncheckedGitHubJson]) -> None:
    lines = [json.dumps(row, ensure_ascii=False) + "\n" for row in rows]
    path.write_text("".join(lines), encoding="utf-8")
