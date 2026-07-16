from app.core.prompt_template import find_template_fields


def test_single_brace_is_a_field():
    assert find_template_fields("Rate: {text}") == {"text"}


def test_double_brace_is_a_literal_not_a_field():
    # str.format_map renders {{x}} as the literal text {x}; it is NOT injected.
    assert find_template_fields("Analyze {{content_markdown}} now") == set()


def test_multiple_and_repeated_fields():
    assert find_template_fields("{a} then {b} then {a}") == {"a", "b"}


def test_attribute_and_index_use_base_name():
    assert find_template_fields("{row.text} and {items[0]}") == {"row", "items"}


def test_positional_fields_are_ignored():
    assert find_template_fields("{} and {0}") == set()


def test_no_placeholder_is_empty():
    assert find_template_fields("just prose") == set()
