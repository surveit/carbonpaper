from docket.citations import Citation, parse_citation


def parsed(raw: str, default_title: int = 40) -> Citation:
    cite = parse_citation(raw, default_title)
    assert cite is not None, f"{raw!r} did not parse as a citation"
    return cite


def test_hyphenated_section_survives() -> None:
    cite = parsed("§ 86.1869-12")
    assert cite.section == "1869-12", f"hyphen suffix lost: {cite.section}"
    assert parsed("§ 86.1869-13").key() != cite.key(), "sibling sections collided"


def test_paragraph_is_kept() -> None:
    assert parsed("§ 1037.150(aa)").paragraph == "(aa)"
    assert parsed("§ 1036.545(g)(4)").paragraph == "(g)(4)"
    assert parsed("§ 1037.150").paragraph is None


def test_explicit_title_beats_default() -> None:
    assert parsed("49 CFR 1037.150").cfr_title == 49
    assert parsed("§ 1037.150").cfr_title == 40


def test_same_section_different_titles_do_not_collide() -> None:
    assert parsed("40 CFR 1037.150").key() != parsed("49 CFR 1037.150").key(), (
        "title dropped from key"
    )


def test_thin_space_after_section_sign() -> None:
    assert parse_citation("§ 085.525", 40) is not None
