"""Architecture: no silent numeric fallback via dict.get (the cardinal rule).

`x.get(k, 1.0)` / `.get(k, 0)` substitutes a made-up number when the key is
absent — the "fabricate instead of fail loud" this project forbids for values
that should come from data. Fix by failing loud (index the key) or returning
None (unknown). A genuinely legitimate numeric default — rare, e.g. a documented
config default that is not itself data — opts out with a trailing
`# data-default-ok: <reason>` on the call's line.
"""
from __future__ import annotations

from arch._helpers import find_numeric_get_defaults, iter_module_files, parse_module

_OPT_OUT = "# data-default-ok"


def test_no_silent_numeric_get_fallback() -> None:
    offenders: list[str] = []
    for path in iter_module_files(""):
        lines = path.read_text(encoding="utf-8").splitlines()
        for start, end in find_numeric_get_defaults(parse_module(path)):
            if not any(_OPT_OUT in line for line in lines[start - 1 : end]):
                offenders.append(f"{path}:{start}  {lines[start - 1].strip()}")
    assert not offenders, (
        "silent numeric .get() fallback - fail loud, return None, or justify with "
        f"`{_OPT_OUT}: <reason>`:\n  " + "\n  ".join(offenders)
    )
