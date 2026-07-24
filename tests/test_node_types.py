from app import models
from app.models.stage import StageType


def test_node_types_match_stage_type_enum() -> None:
    registry_keys = set(models.NODE_TYPES.keys())
    enum_values = {s.value for s in StageType}
    missing_from_registry = enum_values - registry_keys
    missing_from_enum = registry_keys - enum_values
    assert registry_keys == enum_values, (
        f"app.models.NODE_TYPES and StageType enum have drifted. "
        f"In StageType but not NODE_TYPES: {missing_from_registry}. "
        f"In NODE_TYPES but not StageType: {missing_from_enum}."
    )
