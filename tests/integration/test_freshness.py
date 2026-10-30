"""Page verification and freshness, end to end: mark as verified (text kept byte for byte,
commit not reported to tidying), permissions, search, health, home page, web, REST, MCP,
tidying agent, routines and weekly reminder to owners."""

import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.core.errors import ErrorCode, SynaptaneError
from synaptane.services import (
    changes,
    content,
    freshness,
    git_sync,
    health,
    routine_agent,
    spaces,
)
from synaptane.services import search as search_service
from synaptane.services.tidy_tools import Overlay, RunState, Tools
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Space
from tests.integration.conftest import token_for
from tests.integration.gitutil import git
from tests.integration.test_alert_mails import received, smtp  # noqa: F401 — fixture
from tests.integration.test_invitations import Inbox
from tests.integration.test_mcp import Live, live, mcp_calls  # noqa: F401 — fixture
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

PROXMOX = "serveurs/proxmox.md"
# Body of a real page: the front matter weighs almost nothing in it (length counted by BM25).
LOREM = (
    "Vérifier d'abord l'état des liens, puis prévenir l'astreinte. Chaque étape est notée "
    "dans le journal d'exploitation avec l'heure et le nom de la personne qui intervient. "
) * 6


def today(wiki: Wiki) -> date:
    return freshness.today_for(wiki.infra)


def write(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, path: str, text: str
) -> None:
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, text.encode())
    db.commit()


def blob(ctx: Workspace, wiki: Wiki, path: str) -> str:
    data = ctx.local_repository(wiki.infra).blob(f"content/{path}")
    assert data is not None
    return data.decode()


def pending(db: Session, ctx: Workspace, wiki: Wiki) -> list[str]:
    db.expire_all()
    return [i.title for i, _ in changes.pending(db, ctx, wiki.infra)]


def settle(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    """All changes reviewed by tidying: the marker moves forward."""
    head = ctx.local_repository(wiki.infra).head()
    assert head is not None
    found = changes.pending(db, ctx, wiki.infra)
    decisions = {change.path: ("done", "vu", change) for _, change in found}
    changes.record(db, wiki.infra, decisions, head)
    db.flush()
    changes.compact(db, ctx, wiki.infra)
    db.commit()
    assert pending(db, ctx, wiki) == []


def stale_page(owner: str = "alice", days: int = 400) -> str:
    verified = (date.today() - timedelta(days=days)).isoformat()
    return (
        f"---\nowner: {owner}\nverified: {verified}\nreview_every: 1y\n---\n# Vieille\n\nBascule.\n"
    )


# --- Mark as verified ----------------------------------------------------------------------


def test_verifying_keeps_the_text_and_is_not_a_change_for_tidying(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    original = (
        "---\ntitle: Proxmox  # gardé\ntags: [infra, sauvegarde]\n---\n# Proxmox\n\n"
        "La sauvegarde nocturne tourne à 2h.  \n\n| a | b |\n|---|---|\n"
    )
    write(db, database, ctx, wiki, PROXMOX, original)
    settle(db, ctx, wiki)
    result = freshness.verify_page(db, database, wiki.alice, ctx, wiki.infra, PROXMOX)
    db.commit()
    day = today(wiki).isoformat()
    assert result.commit and result.path == PROXMOX
    assert blob(ctx, wiki, PROXMOX) == original.replace(
        "sauvegarde]\n", f"sauvegarde]\nverified: {day}\nowner: alice\n"
    )
    assert result.freshness.is_verified and result.freshness.owner == "alice"
    clone = ctx.clone_path(wiki.infra.id)
    log = git(clone, "log", "-1", "--format=%an|%s|%b")
    assert log.startswith(f"Alice|Page verified: {PROXMOX}|Source: web")
    # Only the verification front matter changed: nothing for tidying to review.
    assert pending(db, ctx, wiki) == []
    # Page without front matter: the block is created at the top, the text follows as is.
    readme = blob(ctx, wiki, "README.md")
    freshness.verify_page(db, database, wiki.alice, ctx, wiki.infra, "README.md")
    db.commit()
    assert blob(ctx, wiki, "README.md") == f"---\nverified: {day}\nowner: alice\n---\n" + readme
    assert pending(db, ctx, wiki) == []
    # A second verification on the same day changes nothing: no commit.
    again = freshness.verify_page(db, database, wiki.alice, ctx, wiki.infra, "README.md")
    assert again.commit is None and again.freshness.is_verified
    # A real change, even verified afterwards, still has to be reviewed.
    write(db, database, ctx, wiki, PROXMOX, blob(ctx, wiki, PROXMOX).replace("2h", "3h"))
    freshness.verify_page(db, database, wiki.alice, ctx, wiki.infra, PROXMOX)
    db.commit()
    assert pending(db, ctx, wiki) == [f"Page changed: {PROXMOX}"]


def test_a_page_verified_again_unchanged_waits_longer(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each verification with no change since the previous one doubles the interval; a change
    (even under a commit message that looks like a verification) or a new interval chosen by
    a person starts again from it."""
    day = date(2026, 1, 5)
    monkeypatch.setattr(freshness, "today_for", lambda space, now=None: day)

    def verify(**kwargs: object) -> freshness.Freshness:
        result = freshness.verify_page(
            db,
            database,
            wiki.alice,
            ctx,
            wiki.infra,
            PROXMOX,
            **kwargs,  # type: ignore[arg-type]
        )
        db.commit()
        return result.freshness

    write(db, database, ctx, wiki, PROXMOX, "---\nreview_every: 3m\n---\n# Proxmox\n\n2h.\n")
    assert (verify().period, verify().reconfirmed) == ("3m", 0)  # first time: nothing to count
    for expected, period in ((1, "6m"), (2, "1y"), (3, "2y"), (4, "2y")):
        day += timedelta(days=30)
        state = verify()
        assert (state.reconfirmed, state.period) == (expected, period)
    assert "reconfirmed: 4\n" in blob(ctx, wiki, PROXMOX)
    assert state.due == date(2028, 5, 5)  # two years at most

    # Changed by an assistant, with a message that imitates a verification: counted as a change.
    page = content.read_file(wiki.alice, ctx, wiki.infra, PROXMOX)
    content.write_file(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        PROXMOX,
        (page.text or "").replace("2h", "3h").encode(),
        message=f"Page verified: {PROXMOX}",
        base_sha=page.sha,
        source="mcp",
    )
    db.commit()
    day += timedelta(days=30)
    state = verify()
    assert (state.reconfirmed, state.period) == (0, "3m")
    assert "reconfirmed" not in blob(ctx, wiki, PROXMOX)

    day += timedelta(days=30)
    assert verify().reconfirmed == 1
    # A person chooses another interval: it applies as is.
    state = verify(review_every="1m", verify=False)
    assert (state.reconfirmed, state.period) == (0, "1m")


def test_a_verification_pushed_from_git_is_not_a_change_either(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    proxmox = wiki.remote.read(f"content/{PROXMOX}")
    assert proxmox is not None
    wiki.remote.push({f"content/{PROXMOX}": "---\nverified: 2026-01-02\n---\n" + proxmox})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert pending(db, ctx, wiki) == []
    wiki.remote.push({"content/README.md": "---\nowner: bob\n---\n# Infra\n\nAutre texte.\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    assert pending(db, ctx, wiki) == ["Page changed: README.md"]


def test_a_page_left_by_tidying_stays_listed_once_verified(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    write(db, database, ctx, wiki, "nouvelle.md", "# Nouvelle\n\nÀ trancher.\n")
    [(_, change)] = changes.pending(db, ctx, wiki.infra)
    head = ctx.local_repository(wiki.infra).head()
    assert head is not None
    changes.record(db, wiki.infra, {change.path: ("left", "Question ouverte", change)}, head)
    db.flush()
    changes.compact(db, ctx, wiki.infra)  # the marker moves past the left page
    db.commit()
    freshness.verify_page(db, database, wiki.alice, ctx, wiki.infra, "nouvelle.md")
    db.commit()
    db.expire_all()
    found = [(i.status, i.title, i.reason) for i, _ in changes.pending(db, ctx, wiki.infra)]
    assert found == [("left", "Page created: nouvelle.md", "Question ouverte")]


def test_rights_refusals_and_settings_without_verifying(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    with pytest.raises(SynaptaneError) as refused:
        freshness.verify_page(db, database, wiki.bob, ctx, wiki.infra, PROXMOX)  # reader
    assert refused.value.code is ErrorCode.FORBIDDEN
    db.rollback()
    for path, code in (("rapport.pdf", ErrorCode.INVALID), ("absente.md", ErrorCode.NOT_FOUND)):
        with pytest.raises(SynaptaneError) as error:
            freshness.verify_page(db, database, wiki.alice, ctx, wiki.infra, path)
        assert error.value.code is code, path
        db.rollback()
    with pytest.raises(SynaptaneError) as invalid:
        freshness.verify_page(db, database, wiki.alice, ctx, wiki.infra, PROXMOX, review_every="x")
    assert invalid.value.code is ErrorCode.INVALID
    # Owner and review interval only, without a date; then the owner is removed.
    result = freshness.verify_page(
        db,
        database,
        wiki.alice,
        ctx,
        wiki.infra,
        PROXMOX,
        owner="Ada Lovelace",
        review_every="6 mois",
        verify=False,
    )
    db.commit()
    text = blob(ctx, wiki, PROXMOX)
    assert text.startswith("---\nowner: Ada Lovelace\nreview_every: 6m\n---\n# Proxmox\n")
    assert result.freshness.state == "never" and result.freshness.period == "6m"
    clone = ctx.clone_path(wiki.infra.id)
    assert git(clone, "log", "-1", "--format=%s") == f"Page tracking updated: {PROXMOX}"
    freshness.verify_page(
        db, database, wiki.alice, ctx, wiki.infra, PROXMOX, owner="", review_every="", verify=False
    )
    db.commit()
    assert blob(ctx, wiki, PROXMOX).startswith("# Proxmox\n")  # block removed: it was empty
    assert pending(db, ctx, wiki) == []


# --- Search -------------------------------------------------------------------------------


def test_search_prefers_verified_pages_without_overriding_relevance(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    body = "Procédure de bascule du routeur de secours, à suivre pas à pas.\n\n" + LOREM
    verified = f"---\nverified: {date.today().isoformat()}\n---\n"
    write(db, database, ctx, wiki, "bascule-a.md", stale_page() + body)
    write(db, database, ctx, wiki, "bascule-b.md", body)
    write(db, database, ctx, wiki, "bascule-c.md", verified + body)
    hits = search_service.search(db, wiki.alice, ctx, "bascule secours")
    order = [h.path for h in hits if h.path.startswith("bascule-")]
    assert order == ["bascule-c.md", "bascule-b.md", "bascule-a.md"]
    by_path = {h.path: h for h in hits}
    assert by_path["bascule-c.md"].verified and not by_path["bascule-c.md"].stale
    assert by_path["bascule-a.md"].stale and not by_path["bascule-a.md"].verified
    assert by_path["bascule-b.md"].freshness == freshness.NEVER_VERIFIED
    # The front matter (owner, verification date) is never part of the snippet.
    for found in ("bascule-a.md", "bascule-c.md"):
        passage = " ".join(by_path[found].passage)
        assert "verified" not in passage and "owner" not in passage and "Procédure" in passage
    # The bonus breaks ties, it does not override relevance: the page that is really about
    # the topic stays ahead, even when stale.
    strong = stale_page() + "# Bascule du secours\n\nBascule du secours : bascule, secours.\n"
    write(db, database, ctx, wiki, "guide.md", strong)
    hits = search_service.search(db, wiki.alice, ctx, "bascule secours")
    assert hits[0].path == "guide.md" and hits[0].stale
    assert search_service.search(db, wiki.alice, ctx, "Kubernetes")[0].freshness is None  # PDF


# --- Health, home page, routines ----------------------------------------------------------


def test_health_lists_pages_to_review_and_pages_without_owner(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    write(db, database, ctx, wiki, "vieille.md", stale_page())
    recent = (date.today() - timedelta(days=200)).isoformat()
    write(db, database, ctx, wiki, "sans.md", f"---\nverified: {recent}\n---\n# Sans\n")
    report = health.space_health(wiki.bob, ctx, wiki.infra)
    [due] = report.due
    assert (due.path, due.owner) == ("vieille.md", "alice")
    assert report.unowned == ["sans.md"] and report.verified == 1
    assert report.problems >= 1
    # With a 6-month space review interval, the page without an interval becomes stale.
    spaces.update_space(db, wiki.root, wiki.infra, review_every="6m")
    db.commit()
    report = health.space_health(wiki.bob, ctx, wiki.infra)
    assert [d.path for d in report.due] == ["vieille.md", "sans.md"]
    assert report.review_every == "6m" and report.verified == 0
    login(client, "alice")
    page = client.get("/o/acme/infra/health").text
    assert "Pages à revoir" in page and "vieille.md" in page and "Alice" in page
    assert "Vérifiées, sans propriétaire" in page and "tous les 6 mois" in page
    api = client.get(
        "/api/v1/organizations/acme/spaces/infra/health",
        headers={"Authorization": f"Bearer {token_for(database, 'bob')}"},
    ).json()
    assert api["counts"]["due"] == 2 and api["counts"]["unowned"] == 1
    assert api["due"][0]["path"] == "vieille.md" and api["due"][0]["owner"] == "alice"
    # Home page: the stale pages one owns, under "En attente".
    home = client.get("/").text
    assert "À revoir\u00a0: vieille.md" in home and "À revoir\u00a0: sans.md" not in home
    login(client, "bob")
    assert "À revoir\u00a0: vieille.md" not in client.get("/").text


def test_coherence_routine_receives_pages_to_review(
    ctx: Workspace, db: Session, database: Database, wiki: Wiki
) -> None:
    write(db, database, ctx, wiki, "vieille.md", stale_page())
    spec = routine_agent.Spec(
        uuid.uuid4(), "Contrôle de cohérence", "coherence", "x", (), "", False, "", False, None
    )
    text = routine_agent.health_text(ctx.local_repository(wiki.infra), spec, space=wiki.infra)
    assert "pages due for review (verification expired): 1" in text
    assert "vieille.md (verified on" in text and "owner: alice" in text


# --- Web ----------------------------------------------------------------------------------


def test_page_badge_menu_and_verification_form(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    login(client, "alice")
    url = f"/o/acme/infra/p/{PROXMOX}"
    page = client.get(url).text
    assert 'data-test="verify-badge"' not in page  # never verified: nothing in the bar
    assert "Jamais vérifiée" in page and 'data-test="verify-page"' in page
    response = client.post(
        "/o/acme/infra/-/verify",
        data={"csrf": csrf(client, url), "path": PROXMOX, "owner": "", "review_every": "3m"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == url
    page = client.get(url).text
    assert "Page marquée comme vérifiée." in page
    badge = page.split('data-test="verify-badge"')[1].split("</span>\n  </span>")[0]
    assert "Vérifiée le" in badge and "Alice" in badge
    assert "tous les 3 mois" in page and "Propriétaire\u00a0: Alice" in page
    assert "owner: alice\nreview_every: 3m" in blob(ctx, wiki, PROXMOX)
    folder = client.get("/o/acme/infra/p/serveurs").text
    assert 'class="verify-mark verify-ok"' in folder
    found = client.get("/search", params={"q": "sauvegarde nocturne"}).text
    assert 'class="verify-mark verify-ok"' in found
    # Reader: sees the status, without the form; if they post anyway, nothing changes.
    login(client, "bob")
    page = client.get(url).text
    assert "Propriétaire\u00a0: Alice" in page and 'data-test="verify-page"' not in page
    before = blob(ctx, wiki, PROXMOX)
    client.post(
        "/o/acme/infra/-/verify",
        data={"csrf": csrf(client, url), "path": PROXMOX, "owner": "bob"},
    )
    assert blob(ctx, wiki, PROXMOX) == before


def test_stale_badge_and_modified_since_verification(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    write(db, database, ctx, wiki, "vieille.md", stale_page())
    login(client, "alice")
    page = client.get("/o/acme/infra/p/vieille.md").text
    assert 'class="verify-badge verify-stale"' in page and "À revoir depuis le" in page
    assert "modifiée depuis la vérification" in page  # last commit after the date
    assert 'class="verify-mark verify-stale"' in client.get("/o/acme/infra").text


def test_space_setting_for_the_default_period(client: TestClient, db: Session, wiki: Wiki) -> None:
    login(client, "root")
    url = "/o/acme/infra/-/settings"
    assert "Pages vérifiées\u00a0: à revoir" in client.get(url).text
    client.post(
        "/o/acme/infra/-/update",
        data={"csrf": csrf(client, url), "name": "Infra", "review_every": "1y"},
    )
    db.expire_all()
    space = db.get(Space, wiki.infra.id)
    assert space is not None and space.review_every == "1y"
    page = client.post(
        "/o/acme/infra/-/update",
        data={"csrf": csrf(client, url), "name": "Infra", "review_every": "souvent"},
    )
    assert "Périodicité invalide" in page.text
    db.expire_all()
    assert db.get(Space, wiki.infra.id).review_every == "1y"  # type: ignore[union-attr]


# --- REST ---------------------------------------------------------------------------------


def test_rest_verify_read_and_search_fields(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    base = "/api/v1/organizations/acme/spaces/infra"
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    bob = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    assert client.post(f"{base}/verify/{PROXMOX}", json={}, headers=bob).status_code == 403
    response = client.post(f"{base}/verify/{PROXMOX}", json={"review_every": "90d"}, headers=alice)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commit"] and body["path"] == PROXMOX
    day = today(wiki)
    assert body["freshness"] == {
        "state": "verified",
        "verified": True,
        "stale": False,
        "verified_at": day.isoformat(),
        "owner": "alice",
        "review_due": (day + timedelta(days=90)).isoformat(),
        "review_every": "90d",
        "reconfirmed": 0,
    }
    read = client.get(f"{base}/files/{PROXMOX}", headers=bob).json()
    assert read["freshness"]["verified"] is True and "verified: " in read["text"]
    assert client.get(f"{base}/files/rapport.pdf", headers=bob).json()["freshness"] is None
    [hit] = [
        h
        for h in client.get(
            "/api/v1/search", params={"q": "sauvegarde nocturne"}, headers=bob
        ).json()
        if h["path"] == PROXMOX
    ]
    assert hit["verified"] is True and hit["stale"] is False
    assert hit["freshness"]["owner"] == "alice"
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    patched = client.patch(base, json={"review_every": "6 mois"}, headers=root)
    assert patched.status_code == 200 and patched.json()["review_every"] == "6m"
    refused = client.post(f"{base}/verify/serveurs/notes.txt", json={}, headers=alice)
    assert refused.status_code == 400


# --- Agents -------------------------------------------------------------------------------


def test_tidy_agent_cannot_erase_or_forge_verification(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    freshness.verify_page(db, database, wiki.alice, ctx, wiki.infra, PROXMOX)
    db.commit()
    verified = blob(ctx, wiki, PROXMOX)
    repo = ctx.local_repository(wiki.infra)
    base = repo.head()
    assert base is not None
    overlay = Overlay(repo, base)
    tools = Tools(overlay, RunState(items={}), ctx, wiki.infra, lambda query: [])
    tools.t_read_page(PROXMOX)
    # Fully rewritten, without the front matter: it is put back, the page stays verified.
    answer = tools.t_write_page(PROXMOX, "# Proxmox\n\nSauvegarde à 3h.\n")
    assert "kept: only a human" in answer
    text = (overlay.read(f"content/{PROXMOX}") or b"").decode()
    assert text == verified.split("# Proxmox")[0] + "# Proxmox\n\nSauvegarde à 3h.\n"
    # Date changed by the agent: restored. Text changed: kept.
    answer = tools.t_edit_page(PROXMOX, "verified: ", "verified: 2099-01-01 # ")
    assert "kept: only a human" in answer
    assert freshness.read_meta(
        (overlay.read(f"content/{PROXMOX}") or b"").decode()
    ).verified == today(wiki)
    assert "kept: only a human" not in tools.t_edit_page(PROXMOX, "3h", "4h")
    # A page created by the agent cannot claim to be verified.
    tools.t_write_page("nouvelle.md", "---\nverified: 2026-01-01\n---\n# Nouvelle\n")
    assert (overlay.read("content/nouvelle.md") or b"").decode() == "# Nouvelle\n"


# --- MCP ----------------------------------------------------------------------------------


def test_mcp_verify_page_and_verification_fields(live: Live) -> None:  # noqa: F811
    alice = token_for(live.database, "alice")
    day = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    infra: dict[str, object] = {"space": "acme/infra"}
    results = mcp_calls(
        live,
        alice,
        ("read", {**infra, "path": "README.md"}),
        ("verify_page", {**infra, "path": "README.md", "review_every": "1y"}),
        ("read", {**infra, "path": "README.md"}),
        ("search", {"query": "sauvegarde"}),
        ("fetch", {"id": "acme/infra/README.md"}),
        ("verify_page", {**infra, "path": "README.md"}),
        (
            "write_page",
            {**infra, "path": "README.md", "content": "# Infra\n\nRéécrite.\n", "base_sha": "x"},
        ),
    )
    assert all(not error for error, _ in results[:6]), results
    assert "verified: never" in results[0][1]
    assert results[1][1].startswith(f"Page verified on {day}, owner: alice, review due")
    assert f"verified: {day}\nowner: alice\nreview_due: " in results[2][1]
    [hit] = json.loads(results[3][1])["results"]
    assert hit["verified"] is True and hit["stale"] is False
    metadata = json.loads(results[4][1])["metadata"]
    assert metadata["verified"] is True and metadata["owner"] == "alice"
    assert metadata["verified_at"] == day and metadata["review_due"]
    assert "already up to date: no commit" in results[5][1]
    assert results[6][0]  # outdated sha: refused, nothing written
    sha = re.search(r"sha: (\w+)", results[2][1])
    assert sha is not None
    [(error, answer)] = mcp_calls(
        live,
        alice,
        (
            "write_page",
            {**infra, "path": "README.md", "content": "# Infra\n\nRéécrite.\n", "base_sha": sha[1]},
        ),
    )
    assert not error and "kept" in answer
    [(_, read)] = mcp_calls(live, alice, ("read", {**infra, "path": "README.md"}))
    assert f"verified: {day}" in read and "Réécrite." in read
    bob = token_for(live.database, "bob")
    [(_, listed)] = mcp_calls(live, bob, ("list_spaces", {}), writer=False)
    assert "reader" in listed


# --- Weekly reminder ----------------------------------------------------------------------


def test_weekly_reminder_to_owners(
    smtp: Inbox,  # noqa: F811
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
) -> None:
    write(db, database, ctx, wiki, "vieille.md", stale_page())
    write(db, database, ctx, wiki, "autre.md", stale_page(owner="personne"))
    user = wiki.alice.user
    user.email, user.email_verified_at = "alice@example.com", datetime.now(UTC)
    db.commit()
    paris = ZoneInfo("Europe/Paris")
    # Monday of this week: the reminder's audit entry is dated now, at most 6 days later (a
    # Monday a week ahead fell outside the quiet period when the test ran on a Monday morning).
    monday = datetime.now(paris).replace(hour=10, minute=0)
    monday -= timedelta(days=monday.weekday())
    saturday = monday + timedelta(days=5)
    assert freshness.send_reminders(database, ctx, now=saturday) == 0  # weekend
    assert freshness.send_reminders(database, ctx, now=monday.replace(hour=20)) == 0  # evening
    assert freshness.send_reminders(database, ctx, now=monday) == 1
    [message] = smtp.messages
    to, subject, body = received(message)
    assert to == "alice@example.com" and subject == "Pages à revoir : 1"
    assert "Infra · vieille.md (à revoir depuis le" in body and "autre.md" not in body
    assert "/o/acme/infra/p/vieille.md" in body
    assert freshness.send_reminders(database, ctx, now=monday + timedelta(hours=2)) == 0  # already
    # Preference unchecked: nothing anymore.
    db.expire_all()
    user = db.merge(user)
    user.notify_routines = False
    db.commit()
    assert freshness.send_reminders(database, ctx, now=monday + timedelta(days=7)) == 0
