"""A space's vocation and tidying rules: repository files, written by the admins (web,
REST), read by tidying and the assistants (MCP)."""

import json
import os
import time
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.config import Settings
from synaptane.core.access import OrgRole
from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.core.secret_scan import scan_text
from synaptane.services import git_sync, organizations, spaces, tidy, vocation
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import AuditEvent, Space
from synaptane.worker.runner import Worker
from tests.integration.conftest import actor_of, make_user, token_for
from tests.integration.gitutil import Remote
from tests.integration.test_mcp import Live, live, mcp_calls  # noqa: F401 — shared fixture
from tests.integration.test_tidy import Scripted, configure_ai, deposit_note
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

VOCATION = "# Vocation\n\n## Ce qu'on y range\n\n- Les serveurs, une fiche par machine.\n"
FAKE_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # shape of an AWS access key


def save(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    text: str,
    kind: vocation.Kind = vocation.VOCATION,
    **options: object,
) -> str | None:
    sha = vocation.save(db, database, wiki.root, ctx, wiki.infra, kind, text, **options)  # type: ignore[arg-type]
    db.commit()
    return sha


# --- Service ----------------------------------------------------------------------------------


def test_saving_commits_in_the_name_of_the_person_and_reaches_the_remote(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    sha = save(db, database, ctx, wiki, VOCATION + "\n\n  ", base_sha="")
    assert sha is not None
    repo = ctx.local_repository(wiki.infra)
    assert vocation.read(repo) == VOCATION.strip()
    assert vocation.read(repo, sha) == VOCATION.strip()
    [last] = repo.history(vocation.VOCATION_FILE, limit=1)
    assert last.author.name == "Root" and last.subject == "Space vocation updated"
    # Same text: no commit. An empty text removes the file.
    assert save(db, database, ctx, wiki, VOCATION) is None
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert wiki.remote.read(vocation.VOCATION_FILE) == VOCATION
    assert save(db, database, ctx, wiki, "  \n") is not None
    assert vocation.read(ctx.local_repository(wiki.infra)) is None
    actions = db.scalars(select(AuditEvent.action).where(AuditEvent.space_id == wiki.infra.id))
    assert "space.vocation_updated" in set(actions)


def test_rules_are_the_file_read_by_the_tidy(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    save(db, database, ctx, wiki, "- Dates au format AAAA-MM-JJ.", vocation.RULES)
    repo = ctx.local_repository(wiki.infra)
    assert repo.blob(tidy.PROMPT_FILE) == b"- Dates au format AAAA-MM-JJ.\n"
    document = vocation.document(wiki.root, ctx, wiki.infra, vocation.RULES)
    assert document.sha == repo.blob_sha(tidy.PROMPT_FILE) and not document.pending
    assert document.history[0].subject == "Tidy-up rules updated"


def test_rules_under_their_old_name_are_still_read_then_renamed_on_save(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Spaces from before the rename: `.synaptane/prompts/rangement.md` is read while it is
    alone; the first save from Synaptane switches to `.synaptane/prompts/tidy.md`."""
    legacy = vocation.LEGACY_RULES_FILE
    wiki.remote.push({legacy: "- Ancienne règle.\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    repo = ctx.local_repository(wiki.infra)
    assert vocation.read_rules(repo) == "- Ancienne règle.\n".encode()  # read by tidying
    document = vocation.document(wiki.root, ctx, wiki.infra, vocation.RULES)
    assert (document.path, document.text) == (legacy, "- Ancienne règle.\n")
    assert document.sha == repo.blob_sha(legacy)
    # The new name wins when both exist.
    wiki.remote.push({vocation.RULES_FILE: "- Nouvelle règle.\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert vocation.read_rules(repo) == "- Nouvelle règle.\n".encode()
    wiki.remote.push({vocation.RULES_FILE: None})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    # Saving writes the new name and removes the old one, in the same commit.
    save(db, database, ctx, wiki, "- Règle revue.", vocation.RULES, base_sha=document.sha)
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert wiki.remote.read(vocation.RULES_FILE) == "- Règle revue.\n"
    assert wiki.remote.read(legacy) is None
    assert vocation.document(wiki.root, ctx, wiki.infra, vocation.RULES).path == (
        vocation.RULES_FILE
    )
    # Clearing the text does not bring the old file back.
    wiki.remote.push({legacy: "- Ancienne règle.\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    save(db, database, ctx, wiki, "", vocation.RULES)
    assert vocation.read_rules(ctx.local_repository(wiki.infra)) is None


def test_secret_size_and_concurrent_change_are_refused(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    with pytest.raises(SynaptaneError) as secret:
        save(db, database, ctx, wiki, f"Clé : {FAKE_KEY}")
    assert secret.value.code == ErrorCode.SECRET
    assert save(db, database, ctx, wiki, f"Clé : {FAKE_KEY}", allow_secrets=True)
    with pytest.raises(SynaptaneError, match="Text too long"):
        save(db, database, ctx, wiki, "x" * (vocation.MAX_LENGTH + 1))
    # Changed by a push since it was read: conflict, nothing is overwritten.
    git_sync.run_sync(database, ctx, wiki.infra.id)
    before = vocation.document(wiki.root, ctx, wiki.infra, vocation.VOCATION).sha
    wiki.remote.push({vocation.VOCATION_FILE: "# Poussée\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    with pytest.raises(SynaptaneError) as changed:
        save(db, database, ctx, wiki, VOCATION, base_sha=before)
    assert changed.value.code == ErrorCode.CONFLICT
    assert vocation.read(ctx.local_repository(wiki.infra)) == "# Poussée"


def test_only_organization_admins_write_and_read_the_rules(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    save(db, database, ctx, wiki, VOCATION)
    for actor in (wiki.alice, wiki.bob):  # contributor, reader
        for kind in (vocation.VOCATION, vocation.RULES):
            with pytest.raises(SynaptaneError) as refused:
                vocation.save(db, database, actor, ctx, wiki.infra, kind, "Autre.")
            assert refused.value.code == ErrorCode.FORBIDDEN
        with pytest.raises(SynaptaneError):
            vocation.document(actor, ctx, wiki.infra, vocation.RULES)
        # Any member can read the vocation: it is served to assistants.
        assert vocation.document(actor, ctx, wiki.infra, vocation.VOCATION).text == VOCATION
    admin = make_user(db, "olga")
    organizations.set_member(db, wiki.root, wiki.infra.organization, admin, OrgRole.ADMIN)
    db.commit()
    olga = actor_of(db, admin)
    assert vocation.save(db, database, olga, ctx, wiki.infra, vocation.RULES, "- Règle.")
    with pytest.raises(SynaptaneError):
        vocation.space_vocation(actor_of(db, make_user(db, "zoe")), ctx, wiki.infra)


def test_templates_are_complete_and_free_of_secrets() -> None:
    assert set(vocation.TEMPLATES) == {
        "infra",
        "marketing",
        "product",
        "client",
        "personal",
        "family",
        "job-search",
        "blank",
    }
    for template in vocation.TEMPLATES.values():
        text = template.text_for("fr")
        assert text.startswith("# Vocation de l'espace\n"), template.key
        for heading in ("## Ce qu'on y range", "## Ce qu'on n'y range pas", "## Conventions"):
            assert heading in text, (template.key, heading)
        assert len(text) < 4000 and scan_text(text) == [], template.key
    with pytest.raises(SynaptaneError, match="Unknown purpose template"):
        vocation.template("cuisine")


def test_templates_exist_in_english_and_french() -> None:
    for template in vocation.TEMPLATES.values():
        english, french = template.text_for("en"), template.text_for("fr")
        assert english.startswith("# Space vocation\n"), template.key
        for heading in ("## What goes here", "## What does not go here", "## Conventions"):
            assert heading in english, (template.key, heading)
        assert len(english) < 4000 and scan_text(english) == [], template.key
        assert french != english
        # Same sections, in the same order, in both languages.
        assert english.count("\n## ") == french.count("\n## "), template.key
        assert template.text_for("de") == english  # any other language: English
    assert vocation.excerpt(vocation.TEMPLATES["infra"].text_for("en"), 40).startswith(
        "What this space is for"
    )


def test_the_language_of_a_space_is_detected_from_its_pages() -> None:
    french = "Le serveur est dans la salle et il faut le redémarrer pour que les mises à jour"
    english = "The server is in the room and it should be restarted for the updates to apply"
    assert vocation.language_of([french * 2]) == "fr"
    assert vocation.language_of([english * 2]) == "en"
    assert vocation.language_of([french * 2, english * 2]) == "fr, en"
    assert vocation.language_of(["Proxmox"]) == ""
    assert vocation.language_note("") == ""
    assert "written in English" in vocation.language_note("en")
    assert "mix French and English" in vocation.language_note("fr, en")


def test_prompt_text_and_excerpt() -> None:
    text = vocation.TEMPLATES["blank"].text_for("fr")
    assert "<!--" not in vocation.for_prompt(text) and "\n\n\n" not in vocation.for_prompt(text)
    assert vocation.for_prompt("a" * (vocation.MAX_LENGTH + 10)).endswith("[… vocation truncated]")
    excerpt = vocation.excerpt(vocation.TEMPLATES["infra"].text_for("fr"), 120)
    assert excerpt.startswith("À quoi sert cet espace") and excerpt.endswith("…")
    assert len(excerpt) == 120 and "\n" not in excerpt


# --- Pending the first sync ------------------------------------------------------------------


def new_space(db: Session, wiki: Wiki, remote: Remote, slug: str = "famille") -> Space:
    space = spaces.create_space(
        db, wiki.root, wiki.infra.organization, slug=slug, name="Famille", repo_url=remote.url
    )
    db.commit()
    return space


def test_template_chosen_at_creation_is_written_by_the_first_sync(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, tmp_path: Path
) -> None:
    remote = Remote(tmp_path / "famille")
    space = new_space(db, wiki, remote)
    vocation.start_with(db, wiki.root, ctx, space, vocation.template("family"))
    db.commit()
    assert vocation.pending(ctx, space)
    waiting = vocation.document(wiki.root, ctx, space, vocation.VOCATION)
    assert waiting.pending and not waiting.synced and waiting.text.startswith("# Vocation")
    # Before the sync, the rules are added to the pending changes, in the admin's name.
    assert vocation.save(db, database, wiki.root, ctx, space, vocation.RULES, "- Règle.") is None
    db.commit()
    git_sync.run_sync(database, ctx, space.id)
    assert not vocation.pending(ctx, space)
    assert remote.read(vocation.VOCATION_FILE) == vocation.template("family").text_for("fr")
    assert remote.read(vocation.RULES_FILE) == "- Règle.\n"
    assert remote.read("content/README.md") == "# Famille\n"  # skeleton still there
    log = remote.last_commit()
    assert log.startswith("Root <") and log.endswith(
        "Synaptane: tidy-up rules and vocation, saved before the first sync"
    )
    assert not vocation.document(wiki.root, ctx, space, vocation.VOCATION).pending


def test_the_template_follows_the_language_of_its_creator(
    db: Session, ctx: Workspace, wiki: Wiki, tmp_path: Path
) -> None:
    space = new_space(db, wiki, Remote(tmp_path / "famille"))
    wiki.root.user.locale = "en"
    vocation.start_with(db, wiki.root, ctx, space, vocation.template("family"))
    waiting = vocation.document(wiki.root, ctx, space, vocation.VOCATION)
    assert waiting.text == vocation.template("family").text_for("en")
    assert waiting.text.startswith("# Space vocation\n")


def test_existing_repository_keeps_its_own_vocation(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, tmp_path: Path
) -> None:
    remote = Remote(tmp_path / "existant")
    remote.push(
        {
            vocation.VOCATION_FILE: "# La nôtre\n",
            vocation.LEGACY_RULES_FILE: "- Les nôtres.\n",  # rules under their old name
            "content/README.md": "# Existant\n",
        }
    )
    space = new_space(db, wiki, remote, "existant")
    vocation.start_with(db, wiki.root, ctx, space, vocation.template("infra"))
    assert vocation.save(db, database, wiki.root, ctx, space, vocation.RULES, "- Règle.") is None
    git_sync.run_sync(database, ctx, space.id)
    assert remote.read(vocation.VOCATION_FILE) == "# La nôtre\n"
    assert remote.read(vocation.RULES_FILE) is None
    assert remote.read(vocation.LEGACY_RULES_FILE) == "- Les nôtres.\n"
    assert not vocation.pending(ctx, space)
    # Unreadable pending file: ignored, then removed.
    broken = vocation.pending_file(ctx.clone_path(space.id))
    broken.write_text("{pas du json", "utf-8")
    assert vocation.apply_pending(ctx.local_repository(space)) is None
    assert not broken.exists()


def test_orphan_pending_texts_are_removed_after_an_hour(
    database: Database, ctx: Workspace, wiki: Wiki, settings: Settings
) -> None:
    gone = ctx.repos_dir / ("0" * 8 + "-0000-4000-8000-" + "0" * 12 + vocation.PENDING_SUFFIX)
    fresh = gone.with_name(gone.name.replace("0000-4000", "1111-4000"))
    for path in (gone, fresh):
        path.write_text("{}", "utf-8")
    old = time.time() - 7200
    os.utime(gone, (old, old))
    Worker(settings, database, ctx).remove_orphan_clones()
    assert not gone.exists() and fresh.exists()
    assert ctx.clone_path(wiki.infra.id).is_dir()  # copies of existing spaces kept


# --- Tidying ----------------------------------------------------------------------------------


def test_tidy_gets_the_vocation_before_the_space_rules(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    deposit_note(db, database, ctx, wiki)
    save(db, database, ctx, wiki, vocation.template("infra").text_for("fr").replace("PRA", "PRAX"))
    save(db, database, ctx, wiki, "Toujours écrire en anglais.", vocation.RULES)
    first, second = Scripted([]), Scripted([])
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=first.factory)
    system = first.system
    assert "Vocation of the space, defined by its administrators" in system
    assert "- PRAX : plan de reprise d'activité." in system
    assert "<!--" not in system and "leave_in_inbox" in system
    assert system.index("PRAX") < system.index("Toujours écrire en anglais.")
    assert "deletion cap" in system.split("Vocation of the space, defined")[1]
    assert "The existing pages of this space are written in French." in system
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=second.factory)
    assert second.system == system  # stable system text: provider cache


def test_tidy_without_vocation_has_no_vocation_block(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    deposit_note(db, database, ctx, wiki)
    script = Scripted([])
    tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert "<vocation>" not in script.system


# --- Web --------------------------------------------------------------------------------------


PAGE = "/o/acme/infra/-/vocation"


def test_settings_page_saves_previews_and_fills_from_a_template(
    client: TestClient, ctx: Workspace, wiki: Wiki
) -> None:
    login(client, "root")
    page = client.get(PAGE)
    assert page.status_code == 200 and "Ce qu&#39;est l&#39;espace" in page.text
    assert "Consignes de rangement" in page.text and "data-texts=" in page.text
    token = csrf(client, PAGE)
    preview = client.post(
        PAGE, data={"csrf": token, "text": "# Titre\n\n<!-- aide -->Corps", "action": "preview"}
    )
    assert preview.status_code == 200 and "pas encore enregistré" in preview.text
    assert "<h1" in preview.text and "aide" not in preview.text.split("vocation-preview")[1]
    assert vocation.space_vocation(wiki.root, ctx, wiki.infra) is None  # nothing saved
    saved = client.post(PAGE, data={"csrf": token, "text": VOCATION, "base_sha": ""})
    assert saved.status_code == 200 and "Vocation enregistrée." in saved.text
    assert vocation.space_vocation(wiki.root, ctx, wiki.infra) == VOCATION.strip()
    assert "Dernières modifications" in saved.text and "Root" in saved.text
    again = client.post(PAGE, data={"csrf": token, "text": "Autre.", "base_sha": ""})
    assert again.status_code == 409 and "Remplacer la version actuelle" in again.text
    sha = vocation.document(wiki.root, ctx, wiki.infra, vocation.VOCATION).sha
    forced = client.post(
        PAGE, data={"csrf": token, "text": "Autre.", "base_sha": "", "overwrite_sha": sha}
    )
    assert "Vocation enregistrée." in forced.text
    secret = client.post(f"{PAGE}/tidy-rules", data={"csrf": token, "text": f"x {FAKE_KEY}"})
    assert secret.status_code == 400 and "Ce n&#39;est pas un secret" in secret.text
    rules = client.post(f"{PAGE}/tidy-rules", data={"csrf": token, "text": "- Règle."})
    assert "Consignes de rangement enregistrées." in rules.text
    rules_sha = vocation.document(wiki.root, ctx, wiki.infra, vocation.RULES).sha
    unchanged = client.post(
        f"{PAGE}/tidy-rules", data={"csrf": token, "text": "- Règle.", "base_sha": rules_sha}
    )
    assert "Rien n&#39;a changé." in unchanged.text
    filled = client.get(PAGE + "?template=marketing")
    assert "Modèle chargé dans l&#39;éditeur" in filled.text
    assert "La plateforme de marque" in filled.text
    unknown = client.get(PAGE + "?template=cuisine")
    assert "Modèle de vocation inconnu" in unknown.text


def test_settings_page_is_for_admins_only(client: TestClient, wiki: Wiki) -> None:
    for username in ("alice", "bob"):
        client.cookies.clear()
        login(client, username)
        assert client.get(PAGE).status_code == 404
        token = csrf(client, "/o/acme/infra")
        refused = client.post(PAGE, data={"csrf": token, "text": "Autre."})
        assert refused.status_code == 404
        assert (
            "Vocation"
            not in client.get("/o/acme/infra/-/members").text.split("settings-nav")[1][:400]
        )


def test_space_created_on_the_web_with_a_template(
    client: TestClient, ctx: Workspace, database: Database, wiki: Wiki, tmp_path: Path
) -> None:
    login(client, "root")
    form = client.get("/o/acme/-/new-space")
    assert "Recherche d&#39;emploi" in form.text
    remote = Remote(tmp_path / "emploi")
    data = {
        "csrf": csrf(client, "/o/acme/-/new-space"),
        "slug": "emploi",
        "name": "Emploi",
        "repo_url": remote.url,
        "vocation_template": "job-search",
    }
    created = client.post("/o/acme/-/spaces", data=data)
    assert created.status_code == 200 and "Vocation de l&#39;espace choisie" in created.text
    pending = client.get("/o/acme/emploi/-/vocation")
    assert "En attente de synchronisation" in pending.text
    saved = client.post(
        "/o/acme/emploi/-/vocation",
        data={"csrf": csrf(client, "/o/acme/emploi/-/vocation"), "text": "# Mon emploi\n"},
    )
    assert "Texte mis en attente" in saved.text
    with database.session() as session:
        space = session.scalar(select(Space).where(Space.slug == "emploi"))
        assert space is not None
        space_id = space.id
    git_sync.run_sync(database, ctx, space_id)
    assert remote.read(vocation.VOCATION_FILE) == "# Mon emploi\n"
    bad = client.post(
        "/o/acme/-/spaces", data={**data, "slug": "autre-emploi", "vocation_template": "x"}
    )
    assert "Modèle de vocation inconnu" in bad.text


# --- REST -------------------------------------------------------------------------------------


API = "/api/v1/organizations/acme/spaces/infra"


def test_rest_reads_and_writes_the_vocation_and_rules(
    client: TestClient, database: Database, wiki: Wiki
) -> None:
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    bob = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    empty = client.get(f"{API}/vocation", headers=bob).json()
    assert empty == {
        "path": vocation.VOCATION_FILE,
        "text": "",
        "sha": None,
        "pending": False,
        "history": [],
    }
    written = client.put(f"{API}/vocation", headers=root, json={"text": VOCATION, "base_sha": ""})
    assert written.status_code == 200 and written.json()["commit"] and not written.json()["pending"]
    read = client.get(f"{API}/vocation", headers=bob).json()
    assert read["text"] == VOCATION and read["history"][0]["author"] == "Root"
    conflict = client.put(f"{API}/vocation", headers=root, json={"text": "B", "base_sha": ""})
    assert conflict.status_code == 409
    assert client.put(f"{API}/vocation", headers=bob, json={"text": "B"}).status_code == 403
    assert client.get(f"{API}/tidy-rules", headers=bob).status_code == 403
    secret = client.put(f"{API}/tidy-rules", headers=root, json={"text": FAKE_KEY})
    assert secret.status_code == 422
    rules = client.put(f"{API}/tidy-rules", headers=root, json={"text": "- Règle."})
    assert rules.status_code == 200
    assert client.get(f"{API}/tidy-rules", headers=root).json()["text"] == "- Règle.\n"
    templates = client.get("/api/v1/vocation-templates", headers=bob).json()
    assert [t["key"] for t in templates][:2] == ["infra", "marketing"]
    assert all(t["text"].startswith("# Vocation") for t in templates)


def test_templates_are_offered_in_the_language_of_the_user(
    client: TestClient, db: Session, database: Database, wiki: Wiki
) -> None:
    wiki.root.user.locale = "en"
    db.commit()
    headers = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    templates = client.get("/api/v1/vocation-templates", headers=headers).json()
    assert all(t["text"].startswith("# Space vocation") for t in templates)
    login(client, "root")
    page = client.get(PAGE + "?template=marketing").text
    assert "The marketing team&#39;s memory" in page and "La plateforme de marque" not in page


def test_rest_puts_text_on_hold_before_the_first_sync(
    client: TestClient, db: Session, database: Database, wiki: Wiki, tmp_path: Path
) -> None:
    new_space(db, wiki, Remote(tmp_path / "attente"), "attente")
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    url = "/api/v1/organizations/acme/spaces/attente/vocation"
    saved = client.put(url, headers=root, json={"text": "# En attente"}).json()
    assert saved == {"commit": None, "pending": True}
    assert client.get(url, headers=root).json()["pending"] is True


def test_mcp_lists_an_excerpt_and_reads_the_whole_vocation(live: Live) -> None:  # noqa: F811
    root = token_for(live.database, "root")
    alice = token_for(live.database, "alice")
    path: dict[str, object] = {"space": "acme/infra", "path": ".synaptane/vocation.md"}
    rules: dict[str, object] = {"space": "acme/infra", "path": ".synaptane/prompts/tidy.md"}
    [(error, before), (_, listed)] = mcp_calls(live, alice, ("read", path), ("list_spaces", {}))
    assert not error and "no vocation defined" in before
    assert "vocation" not in json.loads(listed)[0]
    put = httpx2.put(
        f"{live.url}{API}/vocation",
        headers={"Authorization": f"Bearer {root}"},
        json={"text": vocation.template("infra").text_for("fr")},
    )
    assert put.status_code == 200, put.text
    listed_after, whole, other = mcp_calls(
        live,
        alice,
        ("list_spaces", {}),
        ("read", path),
        ("read", rules),
    )
    excerpt = json.loads(listed_after[1])[0]["vocation"]
    assert excerpt.startswith("À quoi sert cet espace, et pour qui La mémoire technique")
    assert len(excerpt) <= vocation.EXCERPT_LENGTH
    assert not whole[0] and whole[1].startswith("path: .synaptane/vocation.md\n")
    assert "## Vocabulaire" in whole[1] and "PRA :" in whole[1]
    assert other[0]  # the rest of .synaptane/ is not readable by assistants
