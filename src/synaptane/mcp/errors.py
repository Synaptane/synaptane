"""MCP tool errors (in English) that say what to fix.

The SDK renders a validation error in Pydantic jargon, with a documentation URL, and prefixes
every error with "Error executing tool …". An assistant fixes its call better with a sentence
that names the faulty argument and the expected form (bounds, allowed values), taken from the
published schema: the message and the schema cannot contradict each other.
"""

from typing import Any

from pydantic import ValidationError

SDK_PREFIX = "Error executing tool "


def without_prefix(message: str, tool: str) -> str:
    """Message of a deliberate error, without the prefix added by the SDK."""
    return message.removeprefix(f"{SDK_PREFIX}{tool}: ")


def _property(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """Schema of an argument; for an optional argument (`anyOf` with null), its non-null
    form."""
    found: dict[str, Any] = schema.get("properties", {}).get(name, {})
    for option in found.get("anyOf", []):
        if option.get("type") != "null":
            return {**found, **option}
    return found


def _choices(values: list[Any]) -> str:
    quoted = [f'"{v}"' for v in values]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " or " + quoted[-1]


def _bounds(prop: dict[str, Any], ctx: dict[str, Any], unit: str) -> str:
    low = prop.get("minimum", ctx.get("ge"))
    high = prop.get("maximum", ctx.get("le"))
    if low is not None and high is not None:
        return f"{unit} between {low} and {high}"
    if low is not None:
        return f"{unit} greater than or equal to {low}"
    if high is not None:
        return f"{unit} less than or equal to {high}"
    return unit


def _one(item: dict[str, Any], schema: dict[str, Any]) -> str:
    loc = [str(part) for part in item.get("loc", ())]
    name = ".".join(loc) or "arguments"
    prop = _property(schema, loc[0]) if len(loc) == 1 else {}
    kind = str(item.get("type", ""))
    ctx: dict[str, Any] = item.get("ctx") or {}
    where = f"`{name}`"
    if kind == "missing":
        return f"{where} is required"
    if kind in ("int_type", "int_parsing", "int_from_float"):
        return f"{where} must be {_bounds(prop, ctx, 'an integer')}"
    if kind in ("greater_than_equal", "less_than_equal", "greater_than", "less_than"):
        unit = "an integer" if prop.get("type") == "integer" else "a number"
        return f"{where} must be {_bounds(prop, ctx, unit)}"
    if kind in ("literal_error", "enum"):
        values = prop.get("enum")
        if values:
            return f"{where} must be {_choices(values)}"
        expected = str(ctx.get("expected", ""))
        return f"{where} must be {expected}" if expected else f"{where}: value not allowed"
    if kind in ("string_type", "string_unicode"):
        return f"{where} must be a string"
    if kind in ("bool_type", "bool_parsing"):
        return f"{where} must be true or false"
    if kind in ("list_type", "tuple_type"):
        return f"{where} must be a list"
    if kind in ("dict_type", "model_type", "model_attributes_type"):
        return f"{where} must be an object"
    if kind == "string_too_short":
        least = ctx.get("min_length", 1)
        return (
            f"{where} cannot be empty"
            if least == 1
            else f"{where} must contain at least {least} characters"
        )
    if kind == "string_too_long":
        return f"{where} must contain at most {ctx.get('max_length')} characters"
    if kind == "too_long":
        return f"{where} must contain at most {ctx.get('max_length')} items"
    if kind == "extra_forbidden":
        return f"{where}: unknown argument"
    return f"{where}: invalid value"


def explain(error: ValidationError, tool: str, schema: dict[str, Any]) -> str:
    """Arguments rejected by the schema of `tool`, in one sentence, without the received
    values (they are the user's data) or a documentation URL."""
    parts = [_one(dict(item), schema) for item in error.errors()]
    return (
        f"Invalid arguments for {tool}: "
        + "; ".join(dict.fromkeys(parts))
        + ". Fix the call and try again."
    )
