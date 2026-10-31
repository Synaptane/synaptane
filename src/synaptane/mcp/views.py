"""MCP Apps: views an assistant shows in the conversation, in hosts that render them (Claude
on the web and desktop, ChatGPT, VS Code). Each view is bound to a tool (`review`,
`last_tidy`) that also answers in text for the other clients.

A view is one HTML page (`apps/<name>.html`) with the shared style and bridge to the host
inlined here: it holds no data (the host passes it the tool's result), loads nothing from
anywhere, and shows titles and paths as text only.
"""

import json
from functools import cache
from importlib.resources import files
from typing import Any

from mcp.server.apps import EXTENSION_ID

SHARED = ("base.css", "bridge.js")


@cache
def page(name: str) -> str:
    """The view `name`, with the shared files in place of their `/* synaptane:… */` mark."""
    folder = files("synaptane.mcp").joinpath("apps")
    html = folder.joinpath(f"{name}.html").read_text(encoding="utf-8")
    for shared in SHARED:
        mark = f"/* synaptane:{shared} */"
        if mark in html:
            html = html.replace(mark, folder.joinpath(shared).read_text(encoding="utf-8"))
    return html


class AdvertiseApps:
    """Puts the MCP Apps extension in the `initialize` answer (`capabilities.extensions`).

    Hosts render a view only when both sides declare the extension. At the handshake era
    (2025-11-25, what hosts open with today), the SDK writes the answer with that era's schema,
    which has no `extensions` field yet, and drops it. The answer is a single JSON body
    (`json_response=True`): it gets the field back here, nothing else changes."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
        body = b""
        more = True
        received: list[Any] = []
        while more:
            message = await receive()
            received.append(message)
            body += message.get("body", b"")
            more = message.get("more_body", False)
        try:
            initializing = json.loads(body).get("method") == "initialize"
        except (ValueError, AttributeError):
            initializing = False

        async def replay() -> Any:
            return received.pop(0) if received else await receive()

        if not initializing:
            await self.app(scope, replay, send)
            return
        start: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def capture(message: Any) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            payload = b"".join(chunks)
            try:
                answer = json.loads(payload)
                capabilities = answer["result"]["capabilities"]
                capabilities.setdefault("extensions", {}).setdefault(EXTENSION_ID, {})
                payload = json.dumps(answer, ensure_ascii=False).encode()
            except (ValueError, KeyError, TypeError, AttributeError):
                pass  # not a JSON answer to initialize (error, stream): left as is
            headers = [
                (k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(payload)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": payload})

        await self.app(scope, replay, capture)
