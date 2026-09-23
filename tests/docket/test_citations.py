from docket.citations import parse_citation


def test_hyphenated_section_survives() -> None:
    cite = parse_citation("§ 86.1869-12", default_title=40)
    assert cite is not None
    assert cite.section == "1869-12", f"hyphen suffix lost: {cite.section}"
    assert parse_citation("§ 86.1869-13", 40).key() != cite.key(), (
        "sibling sections collided"
    )


def test_paragraph_is_kept() -> None:
    assert parse_citation("§ 1037.150(aa)", 40).paragraph == "(aa)"
    assert parse_citation("§ 1036.545(g)(4)", 40).paragraph == "(g)(4)"
    assert parse_citation("§ 1037.150", 40).paragraph is None


def test_explicit_title_beats_default() -> None:
    assert parse_citation("49 CFR 1037.150", default_title=40).cfr_title == 49
    assert parse_citation("§ 1037.150", default_title=40).cfr_title == 40


def test_same_section_different_titles_do_not_collide() -> None:
    epa = parse_citation("40 CFR 1037.150", 40)
    dot = parse_citation("49 CFR 1037.150", 40)
    assert epa.key() != dot.key(), "title dropped from key"


def test_thin_space_after_section_sign() -> None:
    assert parse_citation("§ 085.525", 40) is not None
