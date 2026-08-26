from __future__ import annotations

from app.services.methodology import read_methodology_headline, write_methodology

# The two shapes, both lifted from a document a run has been exported from.
_UNDER_A_SUBHEADING = """# Venezuela lobbying in the US Senate LDA filings, Q1-Q2 2026

## What this establishes

Which registered lobbying filings for the first two quarters of 2026 concern
Venezuela, who paid for that lobbying, and how much.

## What it leaves out
"""

_STRAIGHT_INTO_PROSE = """# The palm oil mill register

One record per physical mill: its owner, the group above that owner, where it is, and
how much fruit it can process an hour.

## What this method keeps apart
"""


def test_the_title_is_the_documents_own_first_heading():
    write_methodology("proj", _STRAIGHT_INTO_PROSE)

    assert read_methodology_headline("proj").title == "The palm oil mill register"


def test_the_opening_paragraph_is_joined_into_one_line():
    write_methodology("proj", _STRAIGHT_INTO_PROSE)

    assert read_methodology_headline("proj").standfirst == (
        "One record per physical mill: its owner, the group above that owner, where "
        "it is, and how much fruit it can process an hour."
    )


def test_a_subheading_between_the_title_and_the_prose_is_stepped_over():
    write_methodology("proj", _UNDER_A_SUBHEADING)

    headline = read_methodology_headline("proj")
    assert headline.title == (
        "Venezuela lobbying in the US Senate LDA filings, Q1-Q2 2026"
    )
    assert headline.standfirst.startswith("Which registered lobbying filings")


def test_a_project_with_no_document_carries_neither_half():
    headline = read_methodology_headline("proj")

    assert headline.title is None
    assert headline.standfirst is None


def test_prose_before_the_first_heading_means_the_document_titles_nothing():
    write_methodology("proj", "Notes to self.\n\n# The palm oil mill register\n")

    assert read_methodology_headline("proj").title is None


def test_a_list_under_the_heading_is_not_read_as_a_sentence():
    write_methodology("proj", "# The register\n\n- one mill per row\n- one row per mill\n")

    assert read_methodology_headline("proj").standfirst is None
