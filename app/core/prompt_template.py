"""Placeholder analysis for llm_transform prompt templates.

Templates are rendered with `str.format_map`: single-brace `{col}` interpolates,
double-brace `{{col}}` is an ESCAPED literal rendering as `{col}` and never
substitutes. Uses `string.Formatter` so it cannot drift from the renderer."""
from __future__ import annotations

import string


def find_template_fields(template: str) -> set[str]:
    fields: set[str] = set()
    for _text, field_name, _format_spec, _conversion in string.Formatter().parse(template):
        if not field_name:
            continue
        base = field_name.split(".", 1)[0].split("[", 1)[0]
        if base and not base.isdigit():
            fields.add(base)
    return fields
