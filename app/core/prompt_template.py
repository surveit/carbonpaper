"""Placeholder analysis for llm_transform prompt templates.

A prompt_template is rendered per input row with `str.format_map`
(app/runtime/llm.render_prompt): single-brace `{col}` interpolates that row's
`col`; double-brace `{{col}}` is an ESCAPED literal that renders as the text
`{col}` and never substitutes. `find_template_fields` reports which field names
the template would actually interpolate, using the same parser `str.format_map`
uses (`string.Formatter`), so it can never drift from the renderer."""
from __future__ import annotations

import string


def find_template_fields(template: str) -> set[str]:
    """Return the base field names `str.format_map` would interpolate in
    `template`. Escaped braces (`{{ }}`) yield no field; `{a.b}` / `{a[0]}` yield
    the base name `a`; positional `{}` / `{0}` are ignored — a prompt template
    names input columns, not positions."""
    fields: set[str] = set()
    for _text, field_name, _format_spec, _conversion in string.Formatter().parse(template):
        if not field_name:
            continue
        base = field_name.split(".", 1)[0].split("[", 1)[0]
        if base and not base.isdigit():
            fields.add(base)
    return fields
