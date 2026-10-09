"""MCP through the protocol: write and history tools, deactivated account.

Same real HTTP server and same official MCP client as `test_mcp.py`: the effects are then
read in the Git repository, local then remote, not just in the tool's answer.
"""

import re
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from mcp.server.auth.provider import AccessToken
from sqlalchemy import select

from synaptane.config import Settings
from synaptane.core.security import token_hash
from synaptane.mcp.oauth import SCOPE, SynaptaneOAuthProvider
from synaptane.services import accounts, git_sync
from synaptane.storage.models import ApiToken, OAuthToken, Space, User
from tests.integration import test_mcp
from tests.integration.conftest import actor_of, token_for
from tests.integration.gitutil import git, workspace_for
from tests.integration.test_mcp import Live, mcp_calls, oauth_tokens

live = test_mcp.live  # shared fixture: real HTTP server, space acme/infra
INFRA: dict[str, object] = {"space": "acme/infra"}


def infra_id(live: Live) -> UUID:
    with live.database.session() as session:
        space = session.scalar(select(Space).where(Space.slug == "infra"))
        assert space is not None
        return space.id


def ping(live: Live, token: str) -> int:
    """HTTP status of a plain MCP `ping`: authentication happens before any tool."""
    response = httpx2.post(
        f"{live.url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": f"Bearer {token}"},
    )
    return response.status_code


def test_move_delete_history_and_diff_through_mcp(
    live: Live, settings: Settings, tmp_path: Path
) -> None:
    """`move`, `delete`, `history` and `diff` called by an MCP client: the file really moves,
    then disappears from the repository, and the history follows the rename."""
    alice = token_for(live.database, "alice")
    written, edited, moved, read_moved, read_old = mcp_calls(
        live,
        alice,
        ("write_page", {**INFRA, "path": "guides/nas.md", "content": "# NAS\n\n8 To.\n"}),
        ("edit_page", {**INFRA, "path": "guides/nas.md", "old": "8 To", "new": "12 To"}),
        (
            "move",
            {
                **INFRA,
                "source_path": "guides/nas.md",
                "target_path": "content/materiel/nas.md",
                "message": "Range la fiche du NAS",
            },
        ),
        ("read", {**INFRA, "path": "materiel/nas.md"}),
        ("read", {**INFRA, "path": "guides/nas.md"}),
    )
    assert not written[0] and not edited[0], (written, edited)
    assert not moved[0] and moved[1].startswith("Moved to materiel/nas.md. sha: "), moved
    assert not read_moved[0] and "12 To." in read_moved[1]
    assert read_old[0]  # the old path no longer exists

    [(error, history)] = mcp_calls(live, alice, ("history", {**INFRA, "path": "materiel/nas.md"}))
    assert not error, history
    lines = test_mcp.framed(history, "history").strip().split("\n")
    assert len(lines) == 3, history  # write, edit, move: rename followed
    assert all(" Alice — " in line for line in lines)  # author: the person, not the server
    assert lines[0].endswith("— Range la fiche du NAS")
    shas = [line.split(" ", 1)[0] for line in lines]
    assert all(re.fullmatch(r"[0-9a-f]{10}", sha) for sha in shas)

    edit_diff, move_diff, unknown, invalid_sha = mcp_calls(
        live,
        alice,
        ("diff", {**INFRA, "path": "guides/nas.md", "commit": shas[1]}),
        ("diff", {**INFRA, "path": "materiel/nas.md", "commit": shas[0]}),
        ("diff", {**INFRA, "path": "guides/nas.md", "commit": "0" * 40}),
        ("diff", {**INFRA, "path": "guides/nas.md", "commit": "HEAD; rm -rf /"}),
    )
    assert not edit_diff[0] and "-8 To." in edit_diff[1] and "+12 To." in edit_diff[1]
    assert not move_diff[0] and "content/materiel/nas.md" in move_diff[1], move_diff
    assert unknown[0] and "Version not found" in unknown[1]
    assert invalid_sha[0] and "Version not found" in invalid_sha[1]

    deleted, read_deleted, after = mcp_calls(
        live,
        alice,
        ("delete", {**INFRA, "path": "content/materiel/nas.md", "message": "Retire le NAS"}),
        ("read", {**INFRA, "path": "materiel/nas.md"}),
        ("history", {**INFRA, "path": "materiel/nas.md", "limit": 1}),
    )
    assert deleted == (False, "Deleted.")
    assert read_deleted[0]
    assert not after[0]
    assert test_mcp.framed(after[1], "history").strip().endswith("— Retire le NAS")

    # In the working clone: the tool's commits, with their source, and an up-to-date tree.
    ws = workspace_for(settings)
    space_id = infra_id(live)
    clone = ws.clone_path(space_id)
    assert not (clone / "content/guides/nas.md").exists()
    assert not (clone / "content/materiel/nas.md").exists()
    bodies = git(clone, "log", "-2", "--format=%s%n%b%x00").split("\x00")
    assert bodies[0].strip().startswith("Retire le NAS") and "Source: mcp" in bodies[0]
    assert "Via: MCP · tests" in bodies[0]
    assert "Range la fiche du NAS" in bodies[1]
    assert "12 To." in git(clone, "show", "HEAD~1:content/materiel/nas.md")

    # Then on the remote, after the sync these writes queued.
    git_sync.run_sync(live.database, ws, space_id)
    bare = tmp_path / "remote" / "remote.git"
    tree = git(bare, "ls-tree", "-r", "--name-only", "main", "--", "content/")
    assert "content/README.md" in tree and "nas.md" not in tree
    subjects = git(bare, "log", "--format=%s", "-3", "main").split("\n")
    assert subjects[:2] == ["Retire le NAS", "Range la fiche du NAS"]


def test_deactivating_through_the_service_closes_every_mcp_access(live: Live) -> None:
    """Deactivation revokes personal tokens and OAuth connections (`_revoke_everything`):
    neither opens the MCP any more."""
    personal = token_for(live.database, "alice")
    _, tokens, _, _ = oauth_tokens(live)
    assert not mcp_calls(live, personal, ("list_spaces", {}))[0][0]
    assert not mcp_calls(live, tokens["access_token"], ("list_spaces", {}))[0][0]
    with live.database.session() as session:
        root = actor_of(session, accounts.get_user(session, "root"))
        accounts.update_user(session, root, accounts.get_user(session, "alice"), is_active=False)
    assert ping(live, personal) == 401
    assert ping(live, tokens["access_token"]) == 401


def test_deactivated_account_with_unrevoked_tokens_is_refused(live: Live) -> None:
    """Account deactivated directly in the database (restore, script, a future code path that
    forgets): its tokens are not revoked, but none of them may work any more."""
    personal = token_for(live.database, "alice")
    _, tokens, _, _ = oauth_tokens(live)
    with live.database.session() as session:
        alice = session.scalar(select(User).where(User.username == "alice"))
        assert alice is not None
        alice.is_active = False  # bypassing the service, which would revoke everything
    with live.database.session() as session:
        api = session.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(personal)))
        oauth = session.get(OAuthToken, token_hash(tokens["access_token"]))
        assert api is not None and api.revoked_at is None  # tokens still "valid"
        assert oauth is not None and oauth.revoked_at is None
    assert ping(live, personal) == 401
    assert ping(live, tokens["access_token"]) == 401


def test_tool_call_rechecks_the_account_after_authentication(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth (`mcp/server.py`, `call()`): even if authentication let a deactivated
    account through (deactivation between authentication and the tool, or a regression in
    token loading), each tool checks the account again."""

    def stale_personal(self: SynaptaneOAuthProvider, token: str) -> AccessToken | None:
        """Token loading that forgets to check whether the account is active."""
        with self.database.session() as session:
            row = session.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(token)))
            if row is None:
                return None
            return AccessToken(
                token=token,
                client_id="personal-token",
                scopes=[SCOPE],
                resource=self.resource,
                subject=str(row.user_id),
            )

    alice = token_for(live.database, "alice")
    with live.database.session() as session:
        found = session.scalar(select(User).where(User.username == "alice"))
        assert found is not None
        found.is_active = False
    monkeypatch.setattr(SynaptaneOAuthProvider, "_personal", stale_personal)
    listed, written = mcp_calls(
        live,
        alice,
        ("list_spaces", {}),
        ("write_page", {**INFRA, "path": "x.md", "content": "x"}),
        writer=False,  # write tools hidden: `can_write` also checks the account again
    )
    # Server message as is, without the SDK's English prefix.
    assert listed[0] and listed[1] == "Inactive account.", listed
    assert written[0] and written[1] == "Inactive account.", written
