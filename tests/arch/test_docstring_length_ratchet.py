"""Architecture: the prose at a function's, method's, or class's ENTRANCE in ``app/`` and
``tests/`` — its docstring PLUS the comment block above the first statement — at or under 100
characters TOGETHER. One budget over both syntaxes, because prose above the first statement
costs the reader what a docstring costs. `_GRANDFATHERED` / `_GRANDFATHERED_ENTRANCE_PROSE`
(may only shrink) / `_JUSTIFIED_EXCEPTIONS` (rare); else cut it, or move it to docs/.
"""
from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass
from pathlib import Path

import pytest

from arch.test_complexity_ratchet import find_overload_stub_lines
from arch.test_module_docstring_ratchet import find_governed_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_TESTS_ROOT = _REPO_ROOT / "tests"
_PROSE_CHAR_CEILING = 100

_DEFINITION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# Pre-existing symbols over the ceiling when this rule landed. EMPTY: the sweep that
# cut the prose burned every entry down. A ratchet, so entries may only be REMOVED,
# never added — a new offender cuts its prose instead. Keyed on the SYMBOL
# (`path::Qualified.name`), never a line number, which would rot on the next unrelated
# edit; a listed symbol that is gone, or now at or under the ceiling, fails loud as stale.
_GRANDFATHERED: frozenset[str] = frozenset()

# Symbols over the ceiling only once the comment block above their first statement was
# folded into the same budget as their docstring. EMPTY, on the same sweep and the same
# burn-down discipline as `_GRANDFATHERED`: entries may only be REMOVED, and a stale one
# fails loud.
_GRANDFATHERED_ENTRANCE_PROSE: frozenset[str] = frozenset()

# Post-rule exceptions, each mapped to the written reason it earned one. Separate
# from `_GRANDFATHERED` on purpose: that set is a burn-down of pre-existing debt,
# this dict is a deliberate, argued carve-out. Ships EMPTY and should stay very
# rare — the normal remedy is cutting the prose to one short sentence, or moving the
# content to docs/ and referencing the file from the code.
_JUSTIFIED_EXCEPTIONS: dict[str, str] = {}


@dataclass(frozen=True)
class SymbolEntranceProse:
    path: str
    line: int
    symbol: str
    docstring_chars: int
    comment_chars: int

    @property
    def prose_chars(self) -> int:
        return self.docstring_chars + self.comment_chars


def measure_symbol_entrance_prose(paths: list[Path], repo_root: Path) -> list[SymbolEntranceProse]:
    measurements: list[SymbolEntranceProse] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        measurements.extend(
            _measure_children(
                ast.parse(text),
                path.relative_to(repo_root).as_posix(),
                prefix="",
                overload_lines=find_overload_stub_lines(text),
                comment_chars_by_line=read_comment_chars_by_line(text),
            )
        )
    return measurements


def index_by_symbol(measurements: list[SymbolEntranceProse]) -> dict[str, SymbolEntranceProse]:
    """Raises on a duplicate symbol; a ``@property``/``@x.setter`` pair collides."""
    by_symbol: dict[str, SymbolEntranceProse] = {}
    for measurement in measurements:
        key = f"{measurement.path}::{measurement.symbol}"
        if key in by_symbol:
            raise ValueError(
                f"entrance-prose ratchet: two symbols carrying prose resolve to {key} "
                f"(lines {by_symbol[key].line} and {measurement.line}) — the rule keys by "
                "path::Qualified.name and cannot tell them apart, so one would be measured "
                "and the other silently dropped. Give one a distinct name, or — where that "
                "is not an option, as with a @property/@x.setter pair — amend the identity "
                "key in tests/arch/test_docstring_length_ratchet.py"
            )
        by_symbol[key] = measurement
    return by_symbol


def find_ratchet_violations(
    measurements: list[SymbolEntranceProse],
    grandfathered: frozenset[str],
    entrance_prose_grandfathered: frozenset[str],
    exceptions: dict[str, str],
) -> list[str]:
    by_symbol = index_by_symbol(measurements)
    offenders = [
        _describe_violation(by_symbol[key])
        for key in sorted(by_symbol)
        if by_symbol[key].prose_chars > _PROSE_CHAR_CEILING
        and key not in grandfathered
        and key not in entrance_prose_grandfathered
        and key not in exceptions
    ]
    for listed, list_name in (
        (grandfathered, "_GRANDFATHERED"),
        (entrance_prose_grandfathered, "_GRANDFATHERED_ENTRANCE_PROSE"),
        (frozenset(exceptions), "_JUSTIFIED_EXCEPTIONS"),
    ):
        offenders += [
            _describe_stale_entry(key, list_name)
            for key in sorted(listed)
            if _is_stale(key, by_symbol)
        ]
    return offenders


def test_entrance_prose_does_not_exceed_the_ratchet() -> None:
    measurements = measure_symbol_entrance_prose(
        find_governed_files(_APP_ROOT, _TESTS_ROOT), _REPO_ROOT
    )
    offenders = find_ratchet_violations(
        measurements, _GRANDFATHERED, _GRANDFATHERED_ENTRANCE_PROSE, _JUSTIFIED_EXCEPTIONS
    )
    assert not offenders, (
        "entrance-prose ratchet (every function, method, and class under app/ and tests/): "
        "the docstring AND the comment block above the first statement share ONE budget of "
        f"at most {_PROSE_CHAR_CEILING} characters — one short sentence. Moving a docstring "
        "into a comment block at the top of the body does NOT satisfy this rule: the budget "
        "counts both, so the prose has to get shorter, not change syntax. The default is NO "
        "prose at all; it earns its place only by carrying what the code cannot say (an "
        "invariant, a gotcha, units, a non-obvious why). Cut it to one short sentence, or "
        "move the content to a file under docs/ and reference that file from the code. A "
        "comment sitting BESIDE or BELOW the first statement is unbudgeted — that is where "
        "a note about a specific statement belongs. Adding an entry to the "
        "_JUSTIFIED_EXCEPTIONS dict in tests/arch/test_docstring_length_ratchet.py, with a "
        "written reason, should be very rare, and both frozensets beside it may only SHRINK "
        "— never add to either:\n  " + "\n  ".join(offenders)
    )


# --- qualified-name walk ---------------------------------------------------


def _measure_children(
    node: ast.AST,
    rel_path: str,
    prefix: str,
    overload_lines: set[int],
    comment_chars_by_line: dict[int, int],
) -> list[SymbolEntranceProse]:
    measurements: list[SymbolEntranceProse] = []
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, _DEFINITION_NODES):
            measurements.extend(
                _measure_children(child, rel_path, prefix, overload_lines, comment_chars_by_line)
            )
            continue
        symbol = f"{prefix}{child.name}"
        # clean=True dedents, deliberately: a continuation line's leading whitespace is
        # not prose, so a method nested three levels deep is not billed for its indent.
        docstring = ast.get_docstring(child, clean=True)
        docstring_chars = 0 if docstring is None else len(docstring)
        comment_chars = _sum_entrance_comment_chars(child, comment_chars_by_line)
        # An @typing.overload stub is dropped here, BEFORE the identity check, the way
        # the complexity ratchet does it: the stubs and their implementation all resolve
        # to one symbol, and a stub's body is the trivial `...` — nothing to measure.
        # A symbol carrying no prose at all is never recorded, so a list entry naming it
        # reads as stale.
        if docstring_chars + comment_chars > 0 and child.lineno not in overload_lines:
            measurements.append(
                SymbolEntranceProse(
                    rel_path, child.lineno, symbol, docstring_chars, comment_chars
                )
            )
        measurements.extend(
            _measure_children(
                child, rel_path, f"{symbol}.", overload_lines, comment_chars_by_line
            )
        )
    return measurements


def read_comment_chars_by_line(source: str) -> dict[int, int]:
    chars_by_line: dict[int, int] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            chars_by_line[token.start[0]] = len(_strip_comment_marker(token.string))
    return chars_by_line


def _strip_comment_marker(raw: str) -> str:
    body = raw[1:]
    return body[1:] if body.startswith(" ") else body


def _sum_entrance_comment_chars(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    comment_chars_by_line: dict[int, int],
) -> int:
    docstring_node = node.body[0] if ast.get_docstring(node) is not None else None
    remaining = node.body[1:] if docstring_node is not None else node.body
    if not remaining:
        return 0
    lower = node.lineno if docstring_node is None else _end_line_of(docstring_node)
    upper = _first_line_of(remaining[0])
    return sum(chars for line, chars in comment_chars_by_line.items() if lower < line < upper)


def _end_line_of(statement: ast.stmt) -> int:
    if statement.end_lineno is None:
        raise ValueError(
            "entrance-prose ratchet: a docstring statement at line "
            f"{statement.lineno} carries no end_lineno, so the entrance window cannot be "
            "bounded — refusing to guess it"
        )
    return statement.end_lineno


def _first_line_of(statement: ast.stmt) -> int:
    # A decorated def opens at its `@`: a comment above that is not the parent's prose.
    if isinstance(statement, _DEFINITION_NODES) and statement.decorator_list:
        return min(decorator.lineno for decorator in statement.decorator_list)
    return statement.lineno


# --- offender messages -----------------------------------------------------


def _is_stale(key: str, by_symbol: dict[str, SymbolEntranceProse]) -> bool:
    return key not in by_symbol or by_symbol[key].prose_chars <= _PROSE_CHAR_CEILING


def _describe_violation(measurement: SymbolEntranceProse) -> str:
    return (
        f"{measurement.path}::{measurement.symbol}  prose_chars={measurement.prose_chars} "
        f"(docstring {measurement.docstring_chars} + entrance comments "
        f"{measurement.comment_chars} > {_PROSE_CHAR_CEILING}, not listed) — cut it to one "
        "short sentence, or move the content to docs/ and reference that file; moving it "
        "between the two syntaxes buys nothing, they share the budget"
    )


def _describe_stale_entry(key: str, list_name: str) -> str:
    return (
        f"{key}  (no longer over-ceiling entrance prose — the symbol was deleted or renamed, "
        f"or its docstring and entrance comments now total at or under {_PROSE_CHAR_CEILING} "
        f"chars) — delete the stale {list_name} entry"
    )


# --- unit tests for the checker, on tmp_path fixtures (red + green) -------


def _write_module(tmp_path: Path, body: str, name: str = "m.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _measurement(
    symbol: str = "go",
    docstring_chars: int = 150,
    comment_chars: int = 0,
    path: str = "app/m.py",
    line: int = 1,
) -> SymbolEntranceProse:
    return SymbolEntranceProse(
        path=path,
        line=line,
        symbol=symbol,
        docstring_chars=docstring_chars,
        comment_chars=comment_chars,
    )


def test_measure_symbol_entrance_prose_flags_a_function_over_the_ceiling(tmp_path: Path) -> None:
    file = _write_module(tmp_path, f'def go():\n    """{"x" * 101}"""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 101
    offenders = find_ratchet_violations([measurement], frozenset(), frozenset(), {})
    assert len(offenders) == 1
    assert "m.py::go" in offenders[0] and "one short sentence" in offenders[0]


def test_measure_symbol_entrance_prose_passes_a_docstring_of_exactly_the_ceiling(tmp_path: Path) -> None:
    file = _write_module(tmp_path, f'def go():\n    """{"x" * 100}"""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == _PROSE_CHAR_CEILING
    assert find_ratchet_violations([measurement], frozenset(), frozenset(), {}) == []


def test_measure_symbol_entrance_prose_skips_a_symbol_with_no_docstring(tmp_path: Path) -> None:
    file = _write_module(tmp_path, "def go():\n    return 1\n\n\nclass Foo:\n    x = 1\n")
    assert measure_symbol_entrance_prose([file], tmp_path) == []


def test_measure_symbol_entrance_prose_measures_a_class_docstring(tmp_path: Path) -> None:
    file = _write_module(tmp_path, 'class Foo:\n    """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "Foo"
    assert measurement.prose_chars == len("Short.")


def test_measure_symbol_entrance_prose_measures_an_async_function(tmp_path: Path) -> None:
    file = _write_module(tmp_path, 'async def go():\n    """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "go"


def test_measure_symbol_entrance_prose_ignores_the_module_docstring(tmp_path: Path) -> None:
    file = _write_module(tmp_path, f'"""{"x" * 300}"""\ndef go():\n    """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "go"


def test_measure_symbol_entrance_prose_qualifies_a_method_as_class_dot_method(tmp_path: Path) -> None:
    file = _write_module(tmp_path, 'class Foo:\n    def bar(self):\n        """Short."""\n')
    symbols = {m.symbol for m in measure_symbol_entrance_prose([file], tmp_path)}
    assert symbols == {"Foo.bar"}


def test_measure_symbol_entrance_prose_qualifies_a_nested_class_and_closure(tmp_path: Path) -> None:
    file = _write_module(
        tmp_path,
        'class Outer:\n'
        '    """Short."""\n'
        '    class Inner:\n'
        '        """Short."""\n'
        '        def go(self):\n'
        '            """Short."""\n'
        '            def deeper():\n'
        '                """Short."""\n',
    )
    symbols = {m.symbol for m in measure_symbol_entrance_prose([file], tmp_path)}
    assert symbols == {"Outer", "Outer.Inner", "Outer.Inner.go", "Outer.Inner.go.deeper"}


def test_measure_symbol_entrance_prose_finds_a_def_nested_in_a_conditional(tmp_path: Path) -> None:
    file = _write_module(tmp_path, 'if True:\n    def go():\n        """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "go"


def test_measure_symbol_entrance_prose_dedents_a_deeply_nested_docstring(tmp_path: Path) -> None:
    file = _write_module(
        tmp_path,
        "class Foo:\n"
        "    class Bar:\n"
        "        def go(self):\n"
        '            """First.\n'
        "\n"
        '            Second."""\n',
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == len("First.\n\nSecond.")


def test_measure_symbol_entrance_prose_reads_relative_posix_path(tmp_path: Path) -> None:
    nested = tmp_path / "app" / "sub"
    nested.mkdir(parents=True)
    file = _write_module(nested, 'def go():\n    """Short."""\n')
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.path == "app/sub/m.py"


def test_measure_symbol_entrance_prose_excludes_overload_stubs_but_keeps_the_implementation(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "from typing import overload\n"
        "@overload\n"
        'def go(x: int) -> int:\n    """Short."""\n'
        "@overload\n"
        'def go(x: str) -> str:\n    """Short."""\n'
        'def go(x):\n    """Real."""\n',
    )
    measurements = measure_symbol_entrance_prose([file], tmp_path)
    assert [(m.symbol, m.line) for m in measurements] == [("go", 8)]


def test_index_by_symbol_raises_on_a_property_setter_pair(tmp_path: Path) -> None:
    file = _write_module(
        tmp_path,
        "class Foo:\n"
        "    @property\n"
        '    def x(self):\n        """Short."""\n'
        "    @x.setter\n"
        '    def x(self, value):\n        """Short."""\n',
    )
    measurements = measure_symbol_entrance_prose([file], tmp_path)
    with pytest.raises(ValueError, match="m.py::Foo.x") as excinfo:
        index_by_symbol(measurements)
    assert "lines 3 and 6" in str(excinfo.value)


def test_index_by_symbol_keeps_two_different_symbols_in_the_same_file() -> None:
    indexed = index_by_symbol([_measurement(symbol="go"), _measurement(symbol="stop")])
    assert set(indexed) == {"app/m.py::go", "app/m.py::stop"}


def test_find_ratchet_violations_reports_every_offender_sorted_by_symbol() -> None:
    offenders = find_ratchet_violations(
        [_measurement(symbol="zeta"), _measurement(symbol="alpha")], frozenset(), frozenset(), {}
    )
    assert [offender.split()[0] for offender in offenders] == [
        "app/m.py::alpha",
        "app/m.py::zeta",
    ]


def test_find_ratchet_violations_passes_a_grandfathered_symbol_over_the_ceiling() -> None:
    assert find_ratchet_violations([_measurement()], frozenset({"app/m.py::go"}), frozenset(), {}) == []


def test_find_ratchet_violations_passes_a_justified_exception_over_the_ceiling() -> None:
    assert find_ratchet_violations([_measurement()], frozenset(), frozenset(), {"app/m.py::go": "why"}) == []


def test_find_ratchet_violations_still_flags_an_unlisted_symbol_beside_a_listed_one() -> None:
    measurements = [_measurement(symbol="listed"), _measurement(symbol="unlisted")]
    offenders = find_ratchet_violations(measurements, frozenset({"app/m.py::listed"}), frozenset(), {})
    assert len(offenders) == 1
    assert "app/m.py::unlisted" in offenders[0]


def test_find_ratchet_violations_flags_a_grandfathered_entry_now_under_the_ceiling() -> None:
    cut = _measurement(docstring_chars=_PROSE_CHAR_CEILING)
    offenders = find_ratchet_violations([cut], frozenset({"app/m.py::go"}), frozenset(), {})
    assert len(offenders) == 1
    assert "delete the stale _GRANDFATHERED entry" in offenders[0]


def test_find_ratchet_violations_flags_a_grandfathered_entry_for_a_missing_symbol() -> None:
    offenders = find_ratchet_violations([], frozenset({"app/gone.py::go"}), frozenset(), {})
    assert len(offenders) == 1
    assert "app/gone.py::go" in offenders[0] and "_GRANDFATHERED" in offenders[0]


def test_find_ratchet_violations_flags_a_stale_justified_exception() -> None:
    cut = _measurement(docstring_chars=_PROSE_CHAR_CEILING)
    offenders = find_ratchet_violations([cut], frozenset(), frozenset(), {"app/m.py::go": "why"})
    assert len(offenders) == 1
    assert "delete the stale _JUSTIFIED_EXCEPTIONS entry" in offenders[0]


def test_find_ratchet_violations_raises_on_two_symbols_with_the_same_identity() -> None:
    duplicates = [_measurement(line=1), _measurement(line=10, docstring_chars=5)]
    with pytest.raises(ValueError, match="app/m.py::go"):
        find_ratchet_violations(duplicates, frozenset(), frozenset(), {})


def test_justified_exceptions_ships_empty_and_every_entry_carries_a_reason() -> None:
    assert _JUSTIFIED_EXCEPTIONS == {}
    assert all(reason.strip() for reason in _JUSTIFIED_EXCEPTIONS.values())


def test_no_list_exempts_this_rules_own_file() -> None:
    own_file = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    listed = sorted(
        key
        for key in (
            set(_GRANDFATHERED) | set(_GRANDFATHERED_ENTRANCE_PROSE) | set(_JUSTIFIED_EXCEPTIONS)
        )
        if key.split("::", 1)[0] == own_file
    )
    assert not listed, (
        "a rule must never exempt itself: these entries let this file's own entrance prose "
        f"past the {_PROSE_CHAR_CEILING}-character ceiling it enforces on everyone "
        "else. Both frozensets are only for symbols that PREDATE the measure they name, and "
        "this file was written under it — so delete these entries and cut the prose (or drop "
        "it: a well-named checker test rarely needs any):\n  " + "\n  ".join(listed)
    )


def test_grandfathered_entries_are_symbol_keys_not_line_numbers() -> None:
    listed = _GRANDFATHERED | _GRANDFATHERED_ENTRANCE_PROSE
    assert all("::" in key and not key.rsplit("::", 1)[1].isdigit() for key in listed)


def test_the_two_grandfather_lists_do_not_overlap() -> None:
    assert not (_GRANDFATHERED & _GRANDFATHERED_ENTRANCE_PROSE)


def test_the_rule_governs_a_non_empty_set_of_files() -> None:
    governed = {
        path.relative_to(_REPO_ROOT).as_posix()
        for path in find_governed_files(_APP_ROOT, _TESTS_ROOT)
    }
    assert "app/main.py" in governed
    assert "tests/arch/test_docstring_length_ratchet.py" in governed


def test_find_governed_files_raises_when_a_root_has_no_python_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="governs no source files"):
        find_governed_files(tmp_path, tmp_path)


# --- the entrance comment block counts against the same budget -------------


def _comment_block(chars: int, indent: str = "    ") -> str:
    return f"{indent}# {'x' * chars}"


def test_measure_symbol_entrance_prose_flags_an_undocstringed_function_whose_entrance_comment_is_long(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "def go():\n" + _comment_block(55) + "\n" + _comment_block(55) + "\n    return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 110
    assert len(find_ratchet_violations([measurement], frozenset(), frozenset(), {})) == 1


def test_measure_symbol_entrance_prose_adds_the_entrance_comment_to_a_docstring_under_the_ceiling(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        f'def go():\n    """{"d" * 60}"""\n' + _comment_block(60) + "\n    return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 120
    assert len(find_ratchet_violations([measurement], frozenset(), frozenset(), {})) == 1


def test_measure_symbol_entrance_prose_ignores_a_comment_below_the_first_statement(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        f'def go():\n    """{"d" * 60}"""\n    total = 1\n'
        + _comment_block(60)
        + "\n    return total\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 60
    assert find_ratchet_violations([measurement], frozenset(), frozenset(), {}) == []


def test_measure_symbol_entrance_prose_counts_a_comment_above_the_first_field_of_a_class(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "class Foo:\n" + _comment_block(55) + "\n" + _comment_block(55) + "\n    x: int = 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.symbol == "Foo"
    assert measurement.prose_chars == 110


def test_measure_symbol_entrance_prose_counts_an_entrance_comment_split_off_by_a_blank_line(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "def go():\n" + _comment_block(55) + "\n" + _comment_block(55) + "\n\n    return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 110


def test_measure_symbol_entrance_prose_records_nothing_for_a_symbol_with_neither_prose_form(
    tmp_path: Path,
) -> None:
    file = _write_module(tmp_path, "def go():\n    return 1  # trailing note, not the entrance\n")
    assert measure_symbol_entrance_prose([file], tmp_path) == []


def test_measure_symbol_entrance_prose_counts_a_comment_inside_a_multi_line_signature(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "def go(\n" + _comment_block(55) + "\n" + _comment_block(55) + "\n):\n    return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars == 110


def test_measure_symbol_entrance_prose_closes_the_window_at_a_decorated_first_statement(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "class Foo:\n"
        + _comment_block(55)
        + "\n    @property\n"
        + _comment_block(70)
        + "\n    def bar(self):\n        return 1\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert (measurement.symbol, measurement.prose_chars) == ("Foo", 55)


def test_read_comment_chars_by_line_bills_the_prose_a_reader_sees_not_the_marker() -> None:
    chars_by_line = read_comment_chars_by_line("# abc\n#abc\n## abc\nx = 1  # abc\n")
    assert chars_by_line == {1: 3, 2: 3, 3: 5, 4: 3}


def test_find_ratchet_violations_flags_a_stale_entrance_prose_entry() -> None:
    cut = _measurement(docstring_chars=_PROSE_CHAR_CEILING)
    offenders = find_ratchet_violations([cut], frozenset(), frozenset({"app/m.py::go"}), {})
    assert len(offenders) == 1
    assert "delete the stale _GRANDFATHERED_ENTRANCE_PROSE entry" in offenders[0]


def test_measure_symbol_entrance_prose_flags_the_pending_review_shape_that_motivated_the_rule(
    tmp_path: Path,
) -> None:
    file = _write_module(
        tmp_path,
        "class PendingReview:\n"
        '    """One row awaiting a human decision, carried on the deferred marker of the'
        ' row that made it."""\n'
        "\n"
        "    # The key the cache was searched under, and a copy of the row exactly as it\n"
        "    # arrived from upstream.\n"
        "    input_fingerprint: str\n"
        "    frozen_row: dict[str, str]\n",
    )
    [measurement] = measure_symbol_entrance_prose([file], tmp_path)
    assert measurement.prose_chars > _PROSE_CHAR_CEILING
    assert len(find_ratchet_violations([measurement], frozenset(), frozenset(), {})) == 1
