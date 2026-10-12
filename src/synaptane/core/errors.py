"""Business errors shared by every interface (web, REST, MCP)."""

import json
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID = "invalid"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"  # temporary overload: retry in a moment
    SECRET = "secret_detected"  # likely secret in some content: remove it, or confirm
    TOO_MANY = "too_many_requests"  # an account's rate limit: retry later


class Label(str):
    """A message parameter that is itself an interface text ("Title", "attachment {name}"):
    the interface translates it along with the message. Its value is the English text."""

    template: str
    params: dict[str, Any]

    def __new__(cls, template: str, **params: object) -> "Label":
        label = super().__new__(cls, template.format(**params) if params else template)
        label.template, label.params = template, params
        return label

    def dumps(self, limit: int = 300) -> str:
        """Stored form (database): the template and its values, each cut to `limit` characters,
        to translate when shown."""
        params = {
            k: v if isinstance(v, int | float) else str(v)[:limit] for k, v in self.params.items()
        }
        return json.dumps({"template": self.template, "params": params}, ensure_ascii=False)

    @classmethod
    def loads(cls, text: str) -> "Label":
        """Back from `dumps`; any other text (stored before, or by hand) as it is."""
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("template"), str):
            return cls(data["template"], **(data.get("params") or {}))
        return cls(text)


class SynaptaneError(Exception):
    """Expected failure, without sensitive details: the message can be shown to the user.

    `template` (English, the interface's source language) and `params` stay separate so that
    the interface can translate them; `message` is the English text.
    """

    def __init__(self, code: ErrorCode, template: str, **params: object) -> None:
        self.code = code
        self.template = template
        self.params = params
        self.message = template.format(**params) if params else template
        super().__init__(self.message)


def unavailable(template: str, **params: object) -> SynaptaneError:
    return SynaptaneError(ErrorCode.UNAVAILABLE, template, **params)


def too_many(template: str, **params: object) -> SynaptaneError:
    return SynaptaneError(ErrorCode.TOO_MANY, template, **params)


def invalid(template: str, **params: object) -> SynaptaneError:
    return SynaptaneError(ErrorCode.INVALID, template, **params)


def forbidden(
    template: str = "You are not allowed to do this.", **params: object
) -> SynaptaneError:
    return SynaptaneError(ErrorCode.FORBIDDEN, template, **params)


def not_found(template: str = "Not found.", **params: object) -> SynaptaneError:
    return SynaptaneError(ErrorCode.NOT_FOUND, template, **params)


def conflict(template: str, **params: object) -> SynaptaneError:
    return SynaptaneError(ErrorCode.CONFLICT, template, **params)


class BackupError(Exception):
    """Failure of a backup or a restore (command line)."""
