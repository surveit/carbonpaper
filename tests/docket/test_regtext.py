from docket.regtext import _read_verb, _targets_of


def test_multi_section_instruction_captures_every_target() -> None:
    targets = _targets_of("127. Remove §§ 1037.140 and 1037.150.")
    assert targets == ["1037.140", "1037.150"], f"dropped a target: {targets}"


def test_single_section_instruction() -> None:
    assert _targets_of("130. Revise and republish § 1037.150 to read as follows:") == [
        "1037.150"
    ]


def test_hyphenated_section_in_instruction() -> None:
    assert _targets_of("Amend § 86.1869-12 by revising paragraph (b).") == [
        "86.1869-12"
    ]


def test_instruction_without_section_mark_yields_nothing() -> None:
    assert (
        _targets_of("The authority citation for part 60 continues to read as follows:")
        == []
    )


def test_remove_beats_revise_when_both_words_appear() -> None:
    assert _read_verb("Remove §§ 1037.140 and 1037.150.") == "remove"
    assert (
        _read_verb("Revise and republish § 1037.150 to read as follows:")
        == "revise_republish"
    )
