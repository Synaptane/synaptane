"""My account: client label in the assistants' calls table."""

from synaptane.web.account_pages import client_label


def test_client_named_after_token_or_application() -> None:
    tokens, apps = {"Claude Code"}, {"Claude"}
    assert client_label("MCP · Claude Code", tokens, apps) == ("Claude Code", "token")
    assert client_label("MCP · Claude", tokens, apps) == ("Claude", "app")
    # Revoked token or disconnected application: the name alone, with no guessed kind.
    assert client_label("MCP · ancien script", tokens, apps) == ("ancien script", "")
    assert client_label("MCP", tokens, apps) == ("", "")
