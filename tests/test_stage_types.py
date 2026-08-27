from typing import get_args

from app.models.stages import stage_types
from app.models.stage import Stage, StageType


def test_stage_types_match_stage_type_enum() -> None:
    registry_keys = set(stage_types.STAGE_TYPES.keys())
    enum_values = {s.value for s in StageType}
    missing_from_registry = enum_values - registry_keys
    missing_from_enum = registry_keys - enum_values
    assert registry_keys == enum_values, (
        f"app.stage_types.STAGE_TYPES and StageType enum have drifted. "
        f"In StageType but not STAGE_TYPES: {missing_from_registry}. "
        f"In STAGE_TYPES but not StageType: {missing_from_enum}."
    )


def test_every_stage_type_has_exactly_one_model_in_the_stage_union() -> None:
    members = get_args(get_args(Stage)[0])
    tags = [get_args(cls.model_fields["type"].annotation)[0] for cls in members]
    assert sorted(tags) == sorted(StageType)


def test_every_stage_model_names_the_blocks_STAGE_TYPES_advertises() -> None:
    """`report` advertising only `report` is what let the fingerprint miss the code it runs."""
    for cls in get_args(get_args(Stage)[0]):
        stage_type = get_args(cls.model_fields["type"].annotation)[0].value
        # `signature` is required on every stored model but is not a config
        # block — the catalog advertises it as `signature_form` instead.
        required = {
            name for name, field in cls.model_fields.items()
            if field.is_required() and name not in ("id", "description", "type", "signature")
        }
        assert required == set(stage_types.STAGE_TYPES[stage_type].blocks), stage_type


def test_signature_form_matches_each_models_signature_annotation() -> None:
    from app.models.stages.signature import ExtendsSignature, ReplacesSignature

    by_annotation = {ExtendsSignature: "extends", ReplacesSignature: "replaces"}
    for cls in get_args(get_args(Stage)[0]):
        stage_type = get_args(cls.model_fields["type"].annotation)[0].value
        annotated = by_annotation[cls.model_fields["signature"].annotation]
        assert stage_types.STAGE_TYPES[stage_type].signature_form == annotated, stage_type
