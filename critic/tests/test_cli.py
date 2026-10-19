from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from critic.cli import parse_pr_list, parse_pr_range
from critic.labels import load_test_split


def test_pr_numbers_parse_from_a_list_or_a_range() -> None:
    assert parse_pr_list("944,950, 951") == [944, 950, 951]
    assert parse_pr_range("937-940") == [937, 938, 939, 940]
    with pytest.raises(argparse.ArgumentTypeError, match="backwards"):
        parse_pr_range("940-937")


def test_a_split_file_yields_its_test_prs_in_order(tmp_path: Path) -> None:
    split = {
        "definition": "test = PR numbers 937-1096",
        "train": {"prs": [1, 2]},
        "test": {"prs": [976, 944], "prs_with_human_comments": [944]},
    }
    path = tmp_path / "split.json"
    path.write_text(json.dumps(split), encoding="utf-8")
    assert load_test_split(path) == [944, 976]
