"""Monthly AI budget of an organization: spending for the month, alerts, blocking.

Spending is the sum of the month's estimated costs (UTC): tidy and routine runs, and
vectorization of the semantic index (`EMBEDDING_COST` events of the audit log). The
organization's administrators are notified once at 80 %, then once at 100 %; at 100 %, runs
are refused until the next month or until the budget is raised.

A run whose model has no known price, while a budget is set, is counted at the highest price
of the public table (`run_price`): never zero, otherwise the budget would not stop it. The
settings refuse it upstream (`ai_settings`); this fallback covers a model removed from the
table after an update.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Numeric, func, select
from sqlalchemy.orm import Session

from synaptane.ai import pricing
from synaptane.ai.base import SUBSCRIPTION_PROVIDERS
from synaptane.core.access import OrgRole
from synaptane.core.errors import Label
from synaptane.core.security import SecretBox
from synaptane.services import audit, mail, webhooks
from synaptane.storage.models import (
    AuditEvent,
    OrgAISettings,
    Organization,
    OrgMember,
    RoutineRun,
    Space,
    TidyRun,
    User,
)

log = logging.getLogger("synaptane.budget")
LEVELS = (80, 100)
# Billed per token through an API key: without a budget, nothing bounds spending. An
# OpenAI-compatible API is if it has a key; neither Ollama nor subscriptions are.
METERED_PROVIDERS = ("anthropic", "openai", "mistral")
SUGGESTED_BUDGET = Decimal(20)  # € per month, suggested when configuring such a provider
EMBEDDING_COST = "ai.embedding_cost"  # audit event: cost of a semantic indexing
PRUDENT_NOTE = (
    "Unknown price for this model: cost counted at the highest known price ({input} $ / "
    "{output} $ per million tokens) so that the monthly budget applies."
)
# The note as stored in logs (English, or French before the interface texts became English):
# translated when shown (`localize_notes`).
_PRUDENT_NOTES = (
    re.compile(
        r"Unknown price for this model: cost counted at the highest known price "
        r"\((?P<input>[\d.]+) \$ / (?P<output>[\d.]+) \$ per million tokens\) so that the "
        r"monthly budget applies\."
    ),
    re.compile(
        r"Tarif inconnu pour ce modèle : coût compté au tarif le plus élevé connu "
        r"\((?P<input>[\d.]+) \$ / (?P<output>[\d.]+) \$ par million de tokens\) pour que le "
        r"budget mensuel s'applique\."
    ),
)


def localize_notes(text: str, translate: Callable[..., str]) -> str:
    """A log text (run summary, operation detail) with Synaptane's notes translated."""
    for pattern in _PRUDENT_NOTES:
        text = pattern.sub(lambda m: translate(PRUDENT_NOTE, **m.groupdict()), text)
    return text


def metered(provider: str, has_key: bool) -> bool:
    """Provider billed by usage (API key of a paid service)."""
    return provider in METERED_PROVIDERS or (provider == "openai_compatible" and has_key)


@dataclass(frozen=True)
class Spending:
    spent: Decimal
    budget: Decimal | None
    price: pricing.Price | None
    subscription: bool

    @property
    def ratio(self) -> float | None:
        return float(self.spent / self.budget) if self.budget else None

    @property
    def exhausted(self) -> bool:
        return self.budget is not None and self.spent >= self.budget


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent(session: Session, org_id: UUID, now: datetime | None = None) -> Decimal:
    """Spending for the month: tidy and routine runs (`RoutineRun`), embeddings."""
    total = Decimal(0)
    for run in (TidyRun, RoutineRun):
        found = session.scalar(
            select(func.coalesce(func.sum(run.cost), 0))
            .join(Space, Space.id == run.space_id)
            .where(Space.org_id == org_id, run.started_at >= month_start(now))
        )
        total += Decimal(found or 0)
    embedded = session.scalar(
        select(func.coalesce(func.sum(AuditEvent.details["cost"].astext.cast(Numeric)), 0)).where(
            AuditEvent.org_id == org_id,
            AuditEvent.action == EMBEDDING_COST,
            AuditEvent.at >= month_start(now),
        )
    )
    return total + Decimal(embedded or 0)


def record_embeddings(
    session: Session, space: Space, provider: str, model: str, tokens: int, estimated: bool
) -> Decimal | None:
    """Counts the cost of a semantic indexing in the month's budget (if known)."""
    amount = pricing.embedding_cost(provider, model, tokens)
    if amount is None:
        return None
    audit(
        session,
        None,
        EMBEDDING_COST,
        space=space,
        model=model,
        tokens=tokens,
        estimated=estimated,
        cost=amount,
    )
    return amount


def spending(session: Session, org_id: UUID, model: str = "") -> Spending:
    row = session.get(OrgAISettings, org_id)
    price = (
        pricing.price_for(row.provider, model or row.model, row.price_input, row.price_output)
        if row
        else None
    )
    return Spending(
        spent=spent(session, org_id),
        budget=row.monthly_budget if row else None,
        price=price,
        subscription=bool(row and row.provider in SUBSCRIPTION_PROVIDERS),
    )


def run_price(session: Session, org_id: UUID, model: str) -> tuple[pricing.Price | None, str]:
    """Price of a run with this model, and the note to keep in the log if it is a prudent one
    (budget set, unknown price: the highest price of the table, never zero)."""
    state = spending(session, org_id, model)
    if state.price is not None or state.budget is None or state.subscription:
        return state.price, ""
    price = pricing.prudent_price()
    return price, PRUDENT_NOTE.format(input=price.input, output=price.output)


def remaining(session: Session, org_id: UUID) -> Decimal | None:
    """What is left of the month's budget (None: no budget)."""
    row = session.get(OrgAISettings, org_id)
    if row is None or row.monthly_budget is None:
        return None
    return max(row.monthly_budget - spent(session, org_id), Decimal(0))


def refusal(session: Session, org_id: UUID) -> Label | None:
    """Message if the month's budget is used up (the run must not start). Stored in the log
    in English (explained when shown, `tidy_journal`), translated in the alert e-mail."""
    state = spending(session, org_id)
    if not state.exhausted:
        return None
    return Label(
        "Monthly AI budget reached ({spent} € of {budget} €): tidying paused until next month, "
        "or raise the budget (organization AI page).",
        spent=f"{state.spent:.2f}",
        budget=f"{state.budget:.2f}",
    )


def check_alerts(session: Session, box: SecretBox, space: Space, public_url: str = "") -> None:
    """Notifies administrators when crossing 80 % then 100 % (once per month)."""
    row = session.get(OrgAISettings, space.org_id)
    if row is None or not row.monthly_budget:
        return
    month = month_start().strftime("%Y-%m")
    used = spent(session, space.org_id)
    ratio = used / row.monthly_budget * 100
    reached = [level for level in LEVELS if ratio >= level]
    if not reached:
        return
    level = reached[-1]
    done_month, _, done_level = row.budget_alert.partition(":")
    if done_month == month and done_level.isdigit() and int(done_level) >= level:
        return
    row.budget_alert = f"{month}:{level}"
    org = session.get(Organization, space.org_id)
    assert org is not None
    details: dict[str, str | int] = {
        "level": level,
        "spent": f"{used:.2f}",
        "budget": f"{row.monthly_budget:.2f}",
    }
    audit(
        session,
        None,
        "org.ai_budget_alert",
        org_id=org.id,
        level=level,
        spent=details["spent"],
        budget=details["budget"],
    )
    webhooks.emit(session, space, "tidy.budget", details)
    if not mail.configured(session):
        return
    admins = session.scalars(
        select(User)
        .join(OrgMember, OrgMember.user_id == User.id)
        .where(
            OrgMember.org_id == org.id,
            OrgMember.role == OrgRole.ADMIN.value,
            User.is_active.is_(True),
            User.email.is_not(None),
        )
    )
    for admin in admins:
        subject, body = mail.compose(
            "budget",
            admin.locale,
            org=org.name,
            level=level,
            spent=details["spent"],
            budget=details["budget"],
            link=f"{public_url}/o/{org.slug}/-/ai",
        )
        try:
            mail.send(session, box, admin.email or "", subject, body)
        except mail.MailError as error:
            log.warning("Budget alert not sent to %s: %s", admin.email, error)
