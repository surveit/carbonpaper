from __future__ import annotations

import pytest

from critic.corpus import load_pr_listings
from critic.labels import LABEL_DIR, group_labels_by_pr, load_silent_test_prs
from critic.planning import plan_silent_unit
from critic.sampling import find_size_bucket, select_silent_sample
from critic.tests.fixture_data import FIXTURES, PR, REPO, load_labels

SPLIT = LABEL_DIR / "split.json"


def test_silent_test_prs_are_the_test_side_minus_those_with_human_comments() -> None:
    silent = load_silent_test_prs(SPLIT)
    assert len(silent) == 112
    assert PR not in silent and 953 in silent


def test_size_buckets_break_at_50_200_and_600() -> None:
    sizes = [0, 49, 50, 199, 200, 599, 600, 3772]
    assert [find_size_bucket(size) for size in sizes] == [
        "under 50", "under 50", "50-199", "50-199", "200-599", "200-599", "600+", "600+",
    ]


def test_the_sample_takes_evenly_spaced_merged_prs_from_every_bucket() -> None:
    listings = load_pr_listings(FIXTURES / "corpus")
    sample = select_silent_sample(listings, load_silent_test_prs(SPLIT), 5)
    assert sample == [
        940, 942, 943, 953, 961, 980, 987, 991, 992, 997,
        1006, 1020, 1021, 1026, 1029, 1049, 1050, 1076, 1082, 1091,
    ]
    by_number = {item.number: item for item in listings}
    assert all(by_number[pr].mergedAt is not None for pr in sample)
    buckets = [find_size_bucket(by_number[pr].changed_lines) for pr in sample]
    assert {bucket: buckets.count(bucket) for bucket in buckets} == {
        "under 50": 5, "50-199": 5, "200-599": 5, "600+": 5,
    }


def test_a_bucket_short_of_merged_silent_prs_is_refused() -> None:
    listings = load_pr_listings(FIXTURES / "corpus")
    with pytest.raises(ValueError, match=r"bucket 600\+ holds 7 merged silent PRs, fewer than 8"):
        select_silent_sample(listings, load_silent_test_prs(SPLIT), 8)


def test_a_pr_with_human_comments_is_refused_as_silent() -> None:
    labels = load_labels()
    with pytest.raises(ValueError, match="sampled as silent, yet the label set holds"):
        plan_silent_unit(REPO, PR, group_labels_by_pr(labels)[PR], labels.themes)
