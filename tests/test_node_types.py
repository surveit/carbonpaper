from typing import get_args

from app.models.stages import node_types
from app.models.stage import Stage, StageType


def test_node_types_match_stage_type_enum() -> None:
    registry_keys = set(node_types.NODE_TYPES.keys())
    enum_values = {s.value for s in StageType}
    missing_from_registry = enum_values - registry_keys
    missing_from_enum = registry_keys - enum_values
    assert registry_keys == enum_values, (
        f"app.node_types.NODE_TYPES and StageType enum have drifted. "
        f"In StageType but not NODE_TYPES: {missing_from_registry}. "
        f"In NODE_TYPES but not StageType: {missing_from_enum}."
    )


def test_every_stage_type_has_exactly_one_model_in_the_stage_union() -> None:
    """The union is what `parse_stage` dispatches on, so a StageType with no
    member would be a type nothing can parse — and two members claiming the same
    tag is a pydantic error at import, not a silent last-one-wins."""
    members = get_args(get_args(Stage)[0])
    tags = [get_args(cls.model_fields["type"].annotation)[0] for cls in members]
    assert sorted(tags) == sorted(StageType)


def test_every_stage_model_names_the_blocks_NODE_TYPES_advertises() -> None:
    """The prompt copy an authoring agent reads must name exactly the blocks its
    model requires — `publish` advertising only `publish` is what let the
    fingerprint miss the code it runs."""
    for cls in get_args(get_args(Stage)[0]):
        stage_type = get_args(cls.model_fields["type"].annotation)[0].value
        required = {
            name for name, field in cls.model_fields.items()
            if field.is_required() and name not in ("id", "name", "type")
        }
        assert required == set(node_types.NODE_TYPES[stage_type]["blocks"]), stage_type
