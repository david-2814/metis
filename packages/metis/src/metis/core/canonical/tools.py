"""Tool definitions and JSON Schema subset validation.

See canonical-message-format.md §4.4 and §5.4.
"""

from __future__ import annotations

from enum import StrEnum

import msgspec

# JSON Schema subset (canonical-format §5.4):
#   Allowed: basic types, enum, required, properties, items, description, format.
#   Disallowed: $ref, oneOf, anyOf, allOf, not, if/then/else, patternProperties,
#               additionalProperties as a schema (boolean is OK).

_ALLOWED_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "items",
        "description",
        "title",
        "format",
        "default",
        "examples",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "additionalProperties",  # boolean only — enforced below
    }
)

_DISALLOWED_KEYWORDS: frozenset[str] = frozenset(
    {
        "$ref",
        "oneOf",
        "anyOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "patternProperties",
        "dependencies",
        "dependentRequired",
        "dependentSchemas",
        "unevaluatedProperties",
        "unevaluatedItems",
    }
)

_ALLOWED_TYPES: frozenset[str] = frozenset(
    {"string", "number", "integer", "boolean", "null", "object", "array"}
)


class SideEffects(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"


class ToolSchemaError(ValueError):
    """Raised when a tool's input_schema uses disallowed JSON Schema constructs."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_tool_input_schema(schema: dict) -> None:
    """Validate an input_schema against the canonical JSON Schema subset.

    Raises ToolSchemaError if the schema uses disallowed constructs.
    """
    errors: list[str] = []
    _walk_schema(schema, path="$", errors=errors)
    if errors:
        raise ToolSchemaError(errors)


def _walk_schema(node: object, path: str, errors: list[str]) -> None:
    if not isinstance(node, dict):
        return

    for key in node:
        if key in _DISALLOWED_KEYWORDS:
            errors.append(f"{path}: disallowed JSON Schema keyword '{key}'")
        elif key not in _ALLOWED_KEYWORDS:
            errors.append(f"{path}: unknown JSON Schema keyword '{key}'")

    if (t := node.get("type")) is not None:
        types = t if isinstance(t, list) else [t]
        for typ in types:
            if typ not in _ALLOWED_TYPES:
                errors.append(f"{path}.type: '{typ}' not in allowed types {sorted(_ALLOWED_TYPES)}")

    addl = node.get("additionalProperties")
    if addl is not None and not isinstance(addl, bool):
        errors.append(f"{path}.additionalProperties: must be boolean, got schema")

    if (props := node.get("properties")) is not None:
        if not isinstance(props, dict):
            errors.append(f"{path}.properties: must be an object")
        else:
            for prop_name, prop_schema in props.items():
                _walk_schema(prop_schema, f"{path}.properties.{prop_name}", errors)

    if (items := node.get("items")) is not None:
        _walk_schema(items, f"{path}.items", errors)


class ToolDefinition(msgspec.Struct, frozen=True):
    """Tool definition referenced by ToolUseBlock.name.

    The input_schema MUST conform to the canonical JSON Schema subset; call
    `validate_tool_input_schema` at registration time to enforce.
    """

    name: str  # canonical, snake_case, globally unique
    description: str
    input_schema: dict
    side_effects: SideEffects
    requires_workspace: bool = True
