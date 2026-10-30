"""Team prompts: a space's own rituals, written by its administrators, offered to every
assistant over MCP."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.services import team_prompts, vocation
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

MEETING = """---
title: Compte rendu
description: Met en forme des notes de réunion
arguments: sujet, date?
---
Notes de la réunion sur {{sujet}} ({{date}}).
Modèle : {{include serveurs/proxmox.md}}
Absent : {{include nulle-part.md}}
"""


def _save(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, name: str, text: str
) -> None:
    vocation.save(db, database, wiki.root, ctx, wiki.infra, team_prompts.kind(name), text)
    db.commit()


def test_prompts_are_read_from_the_space_and_rendered(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    _save(db, database, ctx, wiki, "compte-rendu", MEETING)
    vocation.save(db, database, wiki.root, ctx, wiki.infra, vocation.RULES, "Règles.")
    db.commit()
    [prompt] = team_prompts.of_space(ctx, wiki.infra)  # tidy.md is not a prompt
    assert (prompt.mcp_name, prompt.mcp_title) == (
        "acme-infra-compte-rendu",
        "Compte rendu (acme/infra)",
    )
    assert [(a.name, a.required) for a in prompt.arguments] == [("sujet", True), ("date", False)]
    rendered = team_prompts.render(wiki.bob, ctx, wiki.infra, prompt, {"sujet": "Proxmox"})
    assert "Notes de la réunion sur Proxmox ()." in rendered.text
    assert 'untrusted="true"' in rendered.text and "sauvegarde nocturne" in rendered.text
    assert "[page not found: nulle-part.md]" in rendered.text
    assert rendered.included == ["serveurs/proxmox.md"]
    assert rendered.text.endswith(team_prompts.UNTRUSTED_NOTE)
    with pytest.raises(SynaptaneError, match="Missing argument: sujet"):
        team_prompts.render(wiki.bob, ctx, wiki.infra, prompt, {})
    for bad in ("tidy", "Avec Espace", "-x", "a" * 41):
        with pytest.raises(SynaptaneError):
            team_prompts.kind(bad)
    # A space the person does not read gives nothing.
    assert team_prompts.available(wiki.alice, ctx, [wiki.other]) == []


def test_admins_write_prompts_in_the_settings(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")  # contributor, not an administrator
    assert client.get("/o/acme/infra/-/prompts").status_code == 404
    login(client, "root")
    page = client.get("/o/acme/infra/-/prompts").text
    assert "Compte rendu de réunion" in page and "Aucun prompt" in page  # the French example
    token = csrf(client, "/o/acme/infra/-/prompts")
    created = client.post(
        "/o/acme/infra/-/prompts", data={"csrf": token, "name": "revue", "text": MEETING}
    )
    assert "Prompt enregistré" in created.text and "/acme-infra-revue" in created.text
    again = client.post(
        "/o/acme/infra/-/prompts", data={"csrf": token, "name": "revue", "text": MEETING}
    )
    assert again.status_code == 400 and "existe déjà" in again.text
    sha = created.text.split('name="base_sha" value="')[1].split('"')[0]
    removed = client.post(
        "/o/acme/infra/-/prompts/revue", data={"csrf": token, "text": "", "base_sha": sha}
    )
    assert "Prompt supprimé" in removed.text and "Aucun prompt" in removed.text
