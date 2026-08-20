"""Architecture: the authoring catalog may not NAME a stage type it does not offer.

A withheld type is withheld on every surface or on none — a note that says "unlike
python_frame_function" advertises a type the model is then refused when it writes one,
and that refusal reads as a bug rather than a rule.
"""
from __future__ import annotations

import re

from app.models.stages.stage_types import AUTHORABLE_TYPES, STAGE_TYPES


def find_withheld_names_in_catalog() -> list[str]:
    withheld = set(STAGE_TYPES) - set(AUTHORABLE_TYPES)
    offenders: list[str] = []
    for name, spec in AUTHORABLE_TYPES.items():
        prose = f"{spec.summary}\n{spec.notes or ''}"
        offenders.extend(
            f"{name}: names withheld type `{other}`"
            # Word-bounded, so `python_row_function` inside `python_row_functions`
            # still matches but `explode` inside `explode_generated` does not.
            for other in sorted(withheld) if re.search(rf"\b{re.escape(other)}\b", prose)
        )
    return offenders


def test_no_offered_type_names_a_withheld_one():
    offenders = find_withheld_names_in_catalog()
    assert not offenders, (
        "a stage type the catalog does not offer must not be named in the prose of one "
        "it does — the model reads the note, writes that type, and is refused on write, "
        "which reads as a bug rather than a rule. Say what the offered type GIVES "
        "instead of what the withheld one costs:\n  " + "\n  ".join(offenders)
    )


def test_the_check_would_catch_a_reintroduction():
    # Grounded: proves the predicate fires, so green means clean prose not an inert scan.
    withheld = sorted(set(STAGE_TYPES) - set(AUTHORABLE_TYPES))
    assert withheld, "nothing is withheld, so this invariant has nothing to protect"
    assert re.search(rf"\b{re.escape(withheld[0])}\b", f"unlike {withheld[0]}, this one")
