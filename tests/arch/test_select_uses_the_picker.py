"""Architecture: a <select> given as the picker() macro's body carries `picker-native`.

static/picker.js finds the select it enhances by that class. Not every <select>
is a picker (run-form.css styles a plain one deliberately), so this governs only
a select inside a `{% call picker(...) %}` block.
"""
from __future__ import annotations

import re

from arch.scope import scan_all_text

_CALL_PICKER_BLOCK = re.compile(
    r"\{%-?\s*call\s+picker\(.*?%\}(.*?)\{%-?\s*endcall\s*-?%\}", re.DOTALL
)
_SELECT_OPEN_TAG = re.compile(r"<select\b[^>]*>")
_CLASS_ATTR = re.compile(r'class="([^"]*)"')
_PICKER_NATIVE = re.compile(r"\bpicker-native\b")


def find_picker_selects_missing_picker_native() -> dict[str, list[str]]:
    offenders: dict[str, list[str]] = {}
    for path in scan_all_text((".html",)):
        text = path.read_text(encoding="utf-8")
        bad = [
            tag
            for block in _CALL_PICKER_BLOCK.findall(text)
            for tag in _SELECT_OPEN_TAG.findall(block)
            if not _has_picker_native(tag)
        ]
        if bad:
            offenders[path.as_posix()] = bad
    return offenders


def _has_picker_native(tag: str) -> bool:
    match = _CLASS_ATTR.search(tag)
    return bool(match and _PICKER_NATIVE.search(match.group(1)))


def test_every_picker_select_carries_picker_native() -> None:
    offenders = find_picker_selects_missing_picker_native()
    assert not offenders, (
        f"<select> inside {{% call picker(...) %}} without class=\"picker-native\": "
        f"{offenders}. static/picker.js finds the select it enhances by that class — "
        "give it the class, or the popover renders with nothing behind it."
    )


def test_the_scan_reaches_the_picker_calls_it_is_meant_to_guard() -> None:
    total = sum(
        len(_SELECT_OPEN_TAG.findall(block))
        for path in scan_all_text((".html",))
        for block in _CALL_PICKER_BLOCK.findall(path.read_text(encoding="utf-8"))
    )
    assert total >= 3, (
        f"found only {total} <select> tags inside {{% call picker(...) %}} blocks — "
        "expected at least the file picker, the eval version picker and the "
        "runs-view picker; a regressed glob/regex would silently guard nothing"
    )


def test_the_pattern_discriminates_compliant_from_not() -> None:
    fixture = (
        '{% call picker("Choose file", size="sm") %}\n'
        '<select id="x" name="x" class="picker-native file-pick"\n'
        '        data-picker-row-action="file-preview">\n'
        "  <option value=\"1\">one</option>\n"
        "</select>\n"
        "{% endcall %}\n"
        '{% call picker("Choose other") %}\n'
        '<select id="y" name="y" class="other-class"></select>\n'
        "{% endcall %}\n"
    )
    blocks = _CALL_PICKER_BLOCK.findall(fixture)
    assert len(blocks) == 2, "the call/endcall pattern matches no fixture block"
    tags = [tag for block in blocks for tag in _SELECT_OPEN_TAG.findall(block)]
    assert len(tags) == 2, "the select pattern matches no fixture tag"
    assert _has_picker_native(tags[0])
    assert not _has_picker_native(tags[1])
