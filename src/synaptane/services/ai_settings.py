"""AI provider of an organization (encrypted key) and effective model of a space."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.ai import pricing
from synaptane.ai.base import (
    DEFAULT_MODELS,
    EFFORTS,
    KEYLESS_PROVIDERS,
    PROVIDERS,
    SUBSCRIPTION_PROVIDERS,
    AIConfig,
    AIError,
)
from synaptane.ai.providers import check_connection
from synaptane.core.access import Action, require
from synaptane.core.errors import Label, forbidden, invalid
from synaptane.core.security import SecretBox
from synaptane.services import Actor, audit, budget, jobs
from synaptane.services.secrets import (
    CUSTOM_URL_REFUSED,
    check_destination,
    get_org_secret,
    put_org_secret,
)
from synaptane.storage.db import Database
from synaptane.storage.models import Job, OrgAISettings, Organization, Space

log = logging.getLogger("synaptane.ai")

AI_KEY = "ai"
# Connection test of a subscription, run by the worker: the CLI and its login only exist
# there (worker volume), not in the web container exposed to the Internet.
AI_TEST = "ai_test"


@dataclass(frozen=True)
class AIView:
    """Displayable settings (never the key)."""

    provider: str
    model: str
    base_url: str
    effort: str
    has_key: bool
    price_input: Decimal | None = None
    price_output: Decimal | None = None
    monthly_budget: Decimal | None = None
    tested_at: datetime | None = None
    test_ok: bool | None = None
    test_message: Label = Label("")
    test_pending: bool = False  # subscription test requested, not yet run by the worker
    metered: bool = False  # billed by usage: without a budget, nothing bounds spending


def amount(value: str, field: str) -> Decimal | None:
    """Amount as typed ("4", "0,80"); empty: None."""
    value = value.strip().replace(",", ".").replace(" ", "").removesuffix("€")
    if not value:
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise invalid("{field}: invalid amount.", field=Label(field)) from None
    if not result.is_finite() or result < 0 or result >= 1_000_000:
        raise invalid("{field}: invalid amount.", field=Label(field))
    return result


def view(session: Session, actor: Actor, box: SecretBox, org: Organization) -> AIView | None:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    row = session.get(OrgAISettings, org.id)
    if row is None:
        return None
    has_key = bool(get_org_secret(session, box, org.id, AI_KEY))
    return AIView(
        row.provider,
        row.model,
        row.base_url,
        row.effort,
        has_key,
        row.price_input,
        row.price_output,
        row.monthly_budget,
        row.tested_at,
        row.test_ok,
        Label.loads(row.test_message),
        pending_test(session, org.id),
        budget.metered(row.provider, has_key),
    )


def configure(
    session: Session,
    actor: Actor,
    box: SecretBox,
    org: Organization,
    *,
    provider: str,
    model: str = "",
    base_url: str = "",
    effort: str = "medium",
    api_key: str | None = None,
    price_input: Decimal | None = None,
    price_output: Decimal | None = None,
    monthly_budget: Decimal | None = None,
) -> AIView:
    """`api_key=None` keeps the stored key; "" removes it. Prices (€ per million tokens):
    both or neither; neither = public price of the model if known."""
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    if provider not in PROVIDERS:
        raise invalid("Unknown provider.")
    if provider in SUBSCRIPTION_PROVIDERS and not actor.access.is_superadmin:
        # The CLI login belongs to the server: only the super-admin assigns it.
        raise forbidden("Only the super admin can use a subscription signed in on the server.")
    model = model.strip() or DEFAULT_MODELS.get(provider, "")
    if not model and provider not in SUBSCRIPTION_PROVIDERS:
        raise invalid("Enter the model to use.")
    base_url = base_url.strip().rstrip("/")
    if provider == "openai_compatible" and not base_url:
        raise invalid("Enter the OpenAI-compatible API address.")
    if base_url and not base_url.startswith(("https://", "http://")):
        raise invalid("Invalid API address.")
    if effort not in EFFORTS:
        raise invalid("Unknown thinking level.")
    if (price_input is None) != (price_output is None):
        raise invalid("Enter both prices (input and output), or neither.")
    if monthly_budget is not None:
        if provider in SUBSCRIPTION_PROVIDERS:
            raise invalid("A subscription has no usage cost: no budget to set.")
        if pricing.price_for(provider, model, price_input, price_output) is None:
            raise invalid(
                "Unknown price for this model: enter the input and output prices so the "
                "monthly budget can apply, or leave the budget empty."
            )
        unpriced = [
            f"{space.name} ({space.ai_model})"
            for space in session.scalars(
                select(Space)
                .where(Space.org_id == org.id, Space.ai_model.is_not(None))
                .order_by(Space.name)
            )
            if space.ai_model
            and pricing.price_for(provider, space.ai_model, price_input, price_output) is None
        ]
        if unpriced:
            raise invalid(
                "Unknown price for the model chosen by these spaces: {spaces}. Enter the input "
                "and output prices, or switch them back to the organization's model, so the "
                "monthly budget can apply.",
                spaces=", ".join(unpriced),
            )
    row = session.get(OrgAISettings, org.id)
    refused = check_destination(
        superadmin=actor.access.is_superadmin,
        old=(row.provider, row.base_url) if row else None,
        new=(provider, base_url),
        has_key=bool(get_org_secret(session, box, org.id, AI_KEY)),
        key_given=api_key is not None,  # new key, or key removed
    )
    if refused:
        raise forbidden(refused) if refused == CUSTOM_URL_REFUSED else invalid(refused)
    if row is None:
        row = OrgAISettings(org_id=org.id, provider=provider, model=model)
        session.add(row)
    if (row.provider, row.model, row.base_url) != (provider, model, base_url) or api_key:
        row.tested_at, row.test_ok, row.test_message = None, None, ""  # to test again
    row.provider, row.model, row.base_url, row.effort = provider, model, base_url, effort
    row.price_input, row.price_output = price_input, price_output
    if row.monthly_budget != monthly_budget:
        row.budget_alert = ""  # new budget: thresholds will be reported again
    row.monthly_budget = monthly_budget
    if api_key is not None:
        put_org_secret(session, box, org.id, AI_KEY, api_key.strip(), actor.user.id)
    audit(session, actor, "org.ai_configured", org_id=org.id, provider=provider, model=model)
    session.flush()
    result = view(session, actor, box, org)
    assert result is not None
    return result


def check_space_model(session: Session, space: Space, model: str) -> None:
    """Refuses for a space a model without a known price when the organization has a budget:
    its cost would not be counted. Prices entered by the organization apply to all its
    models."""
    row = session.get(OrgAISettings, space.org_id)
    if not model or row is None or row.monthly_budget is None:
        return
    if pricing.price_for(row.provider, model, row.price_input, row.price_output) is None:
        raise invalid(
            "Unknown price for the model “{model}” while the organization has a monthly "
            "budget: choose a model with a known price, or have its input and output prices "
            "entered on the organization's AI page.",
            model=model,
        )


def config_for(session: Session, box: SecretBox, space: Space) -> AIConfig | None:
    """Effective configuration of a space: its organization's, with an overridable model."""
    row = session.get(OrgAISettings, space.org_id)
    if row is None:
        return None
    key = get_org_secret(session, box, space.org_id, AI_KEY) or ""
    if not key and row.provider not in KEYLESS_PROVIDERS:
        return None
    return AIConfig(
        provider=row.provider,
        model=space.ai_model or row.model,
        api_key=key,
        base_url=row.base_url,
        effort=row.effort,
    )


def org_config(
    session: Session, actor: Actor, box: SecretBox, org: Organization
) -> AIConfig | None:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    return _org_config(session, box, org.id)


def _org_config(session: Session, box: SecretBox, org_id: UUID) -> AIConfig | None:
    row = session.get(OrgAISettings, org_id)
    if row is None:
        return None
    return AIConfig(
        provider=row.provider,
        model=row.model,
        api_key=get_org_secret(session, box, org_id, AI_KEY) or "",
        base_url=row.base_url,
        effort=row.effort,
    )


def record_test(session: Session, org: Organization, ok: bool, message: str) -> None:
    """Result of the last "Test connection" (status badge of the AI page)."""
    _record_test(session, org.id, ok, message)


def _record_test(session: Session, org_id: UUID, ok: bool, message: str) -> None:
    """The message (a `Label`) is stored with its values, to be shown in each reader's
    language."""
    label = message if isinstance(message, Label) else Label(message)
    row = session.get(OrgAISettings, org_id)
    if row is not None:
        row.tested_at, row.test_ok, row.test_message = datetime.now(UTC), ok, label.dumps()


# --- Subscription test, by the worker ---------------------------------------------------------


def pending_test(session: Session, org_id: UUID) -> bool:
    """True if a subscription test is queued or running in the worker for this organization."""
    found = session.scalar(
        select(Job.id)
        .where(
            Job.kind == AI_TEST,
            Job.status.in_(("queued", "running")),
            Job.payload.contains({"org_id": str(org_id)}),
        )
        .limit(1)
    )
    return found is not None


def request_test(session: Session, actor: Actor, org: Organization) -> bool:
    """Asks the worker to test the subscription; False if a test is already running.

    The result is stored where the AI page reads that of a direct test (`record_test`)."""
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    if pending_test(session, org.id):
        return False
    jobs.enqueue(session, AI_TEST, None, {"org_id": str(org.id), "by": str(actor.user.id)})
    audit(session, actor, "org.ai_test_requested", org_id=org.id)
    return True


def run_test(database: Database, box: SecretBox, org_id: UUID) -> None:
    """Worker job: real test of the subscription (CLI login, tool call), result recorded,
    successful or not. Never retried: the person waiting wants a verdict."""
    with database.session() as session:
        config = _org_config(session, box, org_id)
    if config is None:
        return  # provider removed in the meantime
    message: str
    try:
        ok, message = True, check_connection(config)
    except AIError as error:
        ok, message = False, error.label
    except Exception as error:  # noqa: BLE001 — the page must leave "Test in progress…"
        log.exception("Connection test of organization %s failed", org_id)
        ok, message = False, Label("Unexpected test failure: {error}.", error=type(error).__name__)
    with database.session() as session:
        _record_test(session, org_id, ok, message)
