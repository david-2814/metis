"""Tests for ToolDefinition and the JSON Schema subset validator."""

from __future__ import annotations

import pytest
from metis_core.canonical.tools import (
    SideEffects,
    ToolDefinition,
    ToolSchemaError,
    validate_tool_input_schema,
)


def test_simple_string_schema_valid():
    validate_tool_input_schema({"type": "object", "properties": {"path": {"type": "string"}}})


def test_nested_object_schema_valid():
    validate_tool_input_schema(
        {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "properties": {
                        "depth": {"type": "integer", "minimum": 1, "maximum": 10},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["depth"],
                    "additionalProperties": False,
                }
            },
        }
    )


def test_enum_valid():
    validate_tool_input_schema({"type": "string", "enum": ["read", "write", "execute"]})


@pytest.mark.parametrize(
    "bad_keyword",
    ["$ref", "oneOf", "anyOf", "allOf", "not", "if", "then", "else", "patternProperties"],
)
def test_disallowed_keywords_rejected(bad_keyword):
    schema = {"type": "object", "properties": {"x": {bad_keyword: []}}}
    with pytest.raises(ToolSchemaError) as exc:
        validate_tool_input_schema(schema)
    assert any(bad_keyword in e for e in exc.value.errors)


def test_disallowed_type_rejected():
    with pytest.raises(ToolSchemaError) as exc:
        validate_tool_input_schema({"type": "frobnicate"})
    assert any("frobnicate" in e for e in exc.value.errors)


def test_additional_properties_as_schema_rejected():
    """additionalProperties: boolean is OK; schema is not."""
    with pytest.raises(ToolSchemaError) as exc:
        validate_tool_input_schema({"type": "object", "additionalProperties": {"type": "string"}})
    assert any("additionalProperties" in e for e in exc.value.errors)


def test_additional_properties_boolean_ok():
    validate_tool_input_schema({"type": "object", "additionalProperties": False})
    validate_tool_input_schema({"type": "object", "additionalProperties": True})


def test_multiple_errors_aggregated():
    schema = {
        "$ref": "#/foo",
        "oneOf": [{"type": "string"}],
    }
    with pytest.raises(ToolSchemaError) as exc:
        validate_tool_input_schema(schema)
    assert len(exc.value.errors) >= 2


def test_tool_definition_construct():
    td = ToolDefinition(
        name="read_file",
        description="Read a file from the workspace.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        side_effects=SideEffects.READ,
    )
    assert td.requires_workspace is True
    assert td.side_effects == SideEffects.READ


def test_side_effects_enum_values():
    assert SideEffects.NONE.value == "none"
    assert SideEffects.READ.value == "read"
    assert SideEffects.WRITE.value == "write"
    assert SideEffects.EXECUTE.value == "execute"
    assert SideEffects.NETWORK.value == "network"
