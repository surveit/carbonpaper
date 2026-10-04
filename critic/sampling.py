from __future__ import annotations

from critic.corpus import PullRequestListing

# The size buckets of the label set's stats: a PR's additions plus deletions, each floor inclusive.
SIZE_BUCKETS = (("under 50", 0), ("50-199", 50), ("200-599", 200), ("600+", 600))


def find_size_bucket(changed_lines: int) -> str:
    return next(name for name, floor in reversed(SIZE_BUCKETS) if changed_lines >= floor)


def select_silent_sample(
    listings: list[PullRequestListing], silent_prs: list[int], per_bucket: int
) -> list[int]:
    """Evenly spaced by PR number within each size bucket, so a rerun draws the same PRs."""
    merged = _find_merged_listings(listings, silent_prs)
    sample: list[int] = []
    for name, _ in SIZE_BUCKETS:
        numbers = sorted(item.number for item in merged if find_size_bucket(item.changed_lines) == name)
        if len(numbers) < per_bucket:
            raise ValueError(f"bucket {name} holds {len(numbers)} merged silent PRs, fewer than {per_bucket}")
        sample += [numbers[(2 * i + 1) * len(numbers) // (2 * per_bucket)] for i in range(per_bucket)]
    return sorted(sample)


def _find_merged_listings(
    listings: list[PullRequestListing], silent_prs: list[int]
) -> list[PullRequestListing]:
    """Only a merged PR shows the reviewer let its diff through; an open one may be unread."""
    by_number = {item.number: item for item in listings}
    missing = sorted(set(silent_prs) - set(by_number))
    if missing:
        raise ValueError(f"the PR listing lacks silent PRs {missing}")
    return [by_number[pr] for pr in silent_prs if by_number[pr].mergedAt is not None]
