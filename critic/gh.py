from __future__ import annotations

import json
import shutil
import subprocess

from critic.records import UncheckedGitHubJson


class GitHubCliError(RuntimeError):
    pass


def read_api_object(path: str) -> UncheckedGitHubJson:
    payload = _run_gh(["api", path])
    if not isinstance(payload, dict):
        raise GitHubCliError(f"gh api {path} returned {type(payload).__name__}, not an object")
    return payload


def read_api_list(path: str) -> list[UncheckedGitHubJson]:
    pages = _run_gh(["api", "--paginate", "--slurp", path])
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise GitHubCliError(f"gh api --paginate {path} did not return a list of pages")
    return [item for page in pages for item in page]


def read_graphql(query: str, variables: dict[str, str | int]) -> UncheckedGitHubJson:
    fields = [
        argument
        for name, value in variables.items()
        for argument in ("-F" if isinstance(value, int) else "-f", f"{name}={value}")
    ]
    payload = _run_gh(["api", "graphql", "-f", f"query={query}", *fields])
    if not isinstance(payload, dict) or "data" not in payload:
        raise GitHubCliError(f"gh api graphql returned no data: {payload}")
    return payload


def read_pr_list(repo: str, fields: str, limit: int) -> list[UncheckedGitHubJson]:
    args = ["pr", "list", "-R", repo, "--state", "all", "--limit", str(limit), "--json", fields]
    payload = _run_gh(args)
    if not isinstance(payload, list):
        raise GitHubCliError(f"gh pr list returned {type(payload).__name__}, not a list")
    if len(payload) >= limit:
        raise GitHubCliError(f"gh pr list returned {limit} PRs, its limit: the list may be cut")
    return payload


def _run_gh(args: list[str]) -> object:
    executable = shutil.which("gh")
    if executable is None:
        raise GitHubCliError("the gh CLI is not on PATH")
    completed = subprocess.run(
        [executable, *args], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if completed.returncode != 0:
        raise GitHubCliError(
            f"gh {' '.join(args)} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)
