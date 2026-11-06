"""First steps of a space: worked out from the space, gone after its first real tidy-up."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.services import first_steps, spaces
from synaptane.services.workspace import Workspace
from synaptane.storage.models import TidyRun
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki


def state(steps: list[first_steps.Step] | None) -> dict[str, bool]:
    assert steps is not None
    return {step.key: step.done for step in steps}


def test_each_step_comes_from_the_space(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    steps = first_steps.steps(db, wiki.alice, ctx, ctx.box, wiki.infra)
    assert state(steps) == {
        "repository": True,
        "vocation": False,
        "ai": False,
        "content": True,
        "tidy": False,
    }
    assert steps is not None and steps[-1].url == "/o/acme/infra/tidy"


def test_a_space_without_repository(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    space = spaces.create_space(db, wiki.root, wiki.infra.organization, slug="vide", name="Vide")
    db.commit()
    assert state(first_steps.steps(db, wiki.root, ctx, ctx.box, space)) == {
        "repository": False,
        "vocation": False,
        "ai": False,
        "content": False,
        "tidy": False,
    }


def test_only_for_contributors(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    assert first_steps.steps(db, wiki.bob, ctx, ctx.box, wiki.infra) is None


def test_the_first_tidy_up_that_did_something_ends_them(
    db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    for status in ("failed", "nothing", "running"):
        db.add(TidyRun(space_id=wiki.infra.id, trigger="manual", status=status))
    db.commit()
    assert first_steps.steps(db, wiki.alice, ctx, ctx.box, wiki.infra) is not None
    finished = datetime.now(UTC)
    db.add(TidyRun(space_id=wiki.infra.id, trigger="manual", status="done", finished_at=finished))
    db.commit()
    assert first_steps.steps(db, wiki.alice, ctx, ctx.box, wiki.infra) is None


def test_not_when_the_tidy_up_is_off(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    wiki.infra.tidy_enabled = False
    db.commit()
    assert first_steps.steps(db, wiki.alice, ctx, ctx.box, wiki.infra) is None


def test_shown_on_the_home_of_the_space_only(client: TestClient, wiki: Wiki) -> None:
    login(client, "alice")
    assert "data-first-steps=" in client.get("/o/acme/infra").text
    assert "data-first-steps=" not in client.get("/o/acme/infra/p/serveurs").text
    assert "data-first-steps=" not in client.get("/o/acme/infra/p/README.md").text
