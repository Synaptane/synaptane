"""PostgreSQL tables. Space content lives in Git; the database keeps accounts, permissions,
secrets, tokens and the audit log."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {datetime: DateTime(timezone=True)}


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    # Verified by a link received at this address (or by the invitation, or SSO): only a
    # verified address is used for invitations, SSO linking and forgotten passwords.
    email_verified_at: Mapped[datetime | None]
    password_hash: Mapped[str | None] = mapped_column(Text)  # absent for an SSO-only account
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Language chosen at creation (`accounts.create_user`), then by the person.
    locale: Mapped[str] = mapped_column(String(8))
    # Initial password or reset by an admin: to be changed at the next login.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # Notified by email when the tidy-up handles their notes (filed where, left why).
    notify_tidy: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Receives by email the output of routines that write to the team (weekly summary…).
    notify_routines: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    spaces: Mapped[list["Space"]] = relationship(back_populates="organization")


class OrgMember(Base):
    __tablename__ = "org_members"
    __table_args__ = (CheckConstraint("role IN ('admin', 'member')", name="role"),)

    org_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Space(Base):
    __tablename__ = "spaces"
    __table_args__ = (
        UniqueConstraint("org_id", "slug"),
        CheckConstraint("sync_status IN ('never', 'ok', 'error', 'conflict')", name="sync_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    repo_url: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(String(255), default="main")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # State of the local clone, kept up to date by the sync (worker).
    sync_status: Mapped[str] = mapped_column(String(16), default="never")
    synced_at: Mapped[datetime | None]
    sync_error: Mapped[str | None] = mapped_column(Text)
    # Start of the current error streak: a transient error that lasts is reported.
    sync_error_since: Mapped[datetime | None]
    remote_head: Mapped[str | None] = mapped_column(String(64))
    # Last commit whose content/ changes have been examined by the tidy-up.
    change_watermark: Mapped[str | None] = mapped_column(String(64))
    conflict_paths: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Nightly tidy-up (docs/concepts.md, Tidy-up)
    tidy_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tidy_time: Mapped[str] = mapped_column(String(5), default="02:00")  # HH:MM local
    tidy_timezone: Mapped[str] = mapped_column(String(64), default="Europe/Paris")
    tidy_last_date: Mapped[date | None]  # last (local) day the scheduled pass ran
    # Continuous: a pass N minutes after a drop (None: at the scheduled time only).
    tidy_delay: Mapped[int | None] = mapped_column(Integer)
    ai_model: Mapped[str | None] = mapped_column(String(200))  # otherwise the organization's
    embeddings_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # hybrid search
    # Default review period of pages (`6m`, `1y`…); empty: never due for review
    # (a page can carry its own, `review_every` in its front matter).
    review_every: Mapped[str] = mapped_column(String(16), default="", server_default="")

    organization: Mapped[Organization] = relationship(back_populates="spaces")

    @property
    def address(self) -> str:
        """Readable `organization/space` address (MCP, search, webhooks)."""
        return f"{self.organization.slug}/{self.slug}"


class SpaceMember(Base):
    __tablename__ = "space_members"
    __table_args__ = (CheckConstraint("role IN ('reader', 'contributor')", name="role"),)

    space_id: Mapped[UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class WebSession(Base):
    __tablename__ = "web_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]


class ApiToken(Base):
    """Personal access token (PAT) for the REST API, MCP and scripts."""

    __tablename__ = "api_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hint: Mapped[str] = mapped_column(String(16))  # last characters, for display
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    space_ids: Mapped[list[str] | None] = mapped_column(JSONB)  # None: all, future ones included
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class AssistantUse(Base):
    """Last MCP call of an account per client (OAuth application or token): "Connect my
    assistant" shows it, and the connection test waits for it."""

    __tablename__ = "assistant_uses"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    via: Mapped[str] = mapped_column(String(200), primary_key=True)
    last_at: Mapped[datetime] = mapped_column(default=utcnow)
    calls: Mapped[int] = mapped_column(default=0)


class Secret(Base):
    """Encrypted secret (Git credentials, AI keys…), attached to an organization or a space."""

    __tablename__ = "secrets"
    __table_args__ = (
        CheckConstraint("(org_id IS NULL) <> (space_id IS NULL)", name="owner"),
        Index("uq_secrets_org_purpose", "org_id", "purpose", unique=True),
        Index("uq_secrets_space_purpose", "space_id", "purpose", unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    space_id: Mapped[UUID | None] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(Base):
    """Background job. At most one queued job per (kind, space); the worker claims them with
    `FOR UPDATE SKIP LOCKED` and a lease that expires if the process dies."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="status"),
        Index(
            "uq_jobs_queued",
            "kind",
            "space_id",
            unique=True,
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_jobs_claim", "status", "run_after"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    space_id: Mapped[UUID | None] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    run_after: Mapped[datetime] = mapped_column(default=utcnow)
    locked_until: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class OAuthClient(Base):
    """Dynamically registered MCP client (RFC 7591), e.g. Claude.ai or Claude Desktop."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    info: Mapped[dict[str, object]] = mapped_column(JSONB)  # OAuthClientInformationFull
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class OAuthRequest(Base):
    """Authorization request awaiting the user's consent (10 minutes)."""

    __tablename__ = "oauth_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    params: Mapped[dict[str, object]] = mapped_column(JSONB)
    expires_at: Mapped[datetime]


class OAuthCode(Base):
    __tablename__ = "oauth_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    params: Mapped[dict[str, object]] = mapped_column(JSONB)
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)  # chosen at consent
    space_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime]


class OAuthToken(Base):
    """Hashed OAuth token. A `family` groups the tokens of one authorization: revoking it
    disconnects the application."""

    __tablename__ = "oauth_tokens"
    __table_args__ = (CheckConstraint("kind IN ('access', 'refresh')", name="kind"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    family: Mapped[UUID] = mapped_column(index=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    resource: Mapped[str | None] = mapped_column(Text)
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    space_ids: Mapped[list[str] | None] = mapped_column(JSONB)  # None: all, future ones included
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class OrgAISettings(Base):
    """AI provider of an organization. The key is a `Secret` (purpose "ai")."""

    __tablename__ = "org_ai_settings"

    org_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(Text, default="")
    effort: Mapped[str] = mapped_column(String(16), default="medium")
    # Price in € per million tokens; empty: the model's public price if known.
    price_input: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    price_output: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    monthly_budget: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # €; empty: none
    budget_alert: Mapped[str] = mapped_column(String(16), default="")  # "2026-10:80": alert sent
    # Last "Test connection": the status badge shows health, not just configuration.
    tested_at: Mapped[datetime | None]
    test_ok: Mapped[bool | None] = mapped_column(Boolean)
    test_message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class OrgEmbeddingSettings(Base):
    """Embedding provider of an organization. The key is a `Secret` (purpose "embedding")."""

    __tablename__ = "org_embedding_settings"

    org_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(Text, default="")
    # Semantic search: similarity the best page must reach for meaning to count (its scale
    # depends on the model). None: it must stand out from the median of the others instead.
    meaning_min_similarity: Mapped[float | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class EmbeddingChunk(Base):
    """A page chunk and its vector. Can be rebuilt from Git (`synaptane reindex`)."""

    __tablename__ = "embedding_chunks"
    __table_args__ = (Index("ix_embedding_chunks_space_path", "space_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)  # relative to content/
    blob_sha: Mapped[str] = mapped_column(String(64))
    chunk: Mapped[int]
    heading: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    embedding: Mapped[list[float]] = mapped_column(Vector())


class TidyRun(Base):
    """A tidy-up agent pass over a space's inbox."""

    __tablename__ = "tidy_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'done', 'nothing', 'failed')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(16))  # schedule or manual
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default="running")
    model: Mapped[str] = mapped_column(String(200), default="")
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    items_done: Mapped[int] = mapped_column(default=0)
    items_left: Mapped[int] = mapped_column(default=0)
    operations: Mapped[list[dict[str, object]]] = mapped_column(JSONB, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    commit: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cached_tokens: Mapped[int] = mapped_column(default=0)  # input read back from the cache
    cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))  # estimated €; None: subscription
    # Subscription: used share of the windows (5 h, 7 days) at start and end (ai.quota).
    quota: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    # Quality of the pass, measured without AI before the commit (services.tidy_metrics).
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    reverted_at: Mapped[datetime | None]
    reverted_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Routine(Base):
    """Agent routine of a space (`services.routines`): instructions, a schedule, a scope and
    outputs. The agent reads the wiki; it only writes to its outputs (report page, inbox,
    email, webhook)."""

    __tablename__ = "routines"
    __table_args__ = (
        UniqueConstraint("space_id", "name"),
        CheckConstraint("frequency IN ('daily', 'weekly', 'monthly')", name="frequency"),
        CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name="weekday"),
        CheckConstraint("monthday IS NULL OR monthday BETWEEN 1 AND 28", name="monthday"),
        CheckConstraint("output_email IN ('', 'admins', 'members')", name="output_email"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    template: Mapped[str] = mapped_column(String(32), default="")  # original template; "": blank
    instructions: Mapped[str] = mapped_column(Text, default="")
    # Schedule, in the space's local time (`Space.tidy_timezone`).
    frequency: Mapped[str] = mapped_column(String(8), default="weekly")
    weekday: Mapped[int | None] = mapped_column(Integer)  # weekly: 0 = Monday
    monthday: Mapped[int | None] = mapped_column(Integer)  # monthly: 1 to 28
    at: Mapped[str] = mapped_column(String(5), default="06:00")  # HH:MM local
    scope: Mapped[list[str]] = mapped_column(JSONB, default=list)  # folders; empty: everything
    # Outputs: page rewritten on each pass (relative to content/; "": none), items dropped
    # into the inbox, email (organization admins or space members), webhook.
    output_page: Mapped[str] = mapped_column(Text, default="")
    output_inbox: Mapped[bool] = mapped_column(Boolean, default=False)
    output_email: Mapped[str] = mapped_column(String(16), default="")
    output_webhook: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_date: Mapped[date | None]  # last (local) day the scheduled pass was requested
    last_run_at: Mapped[datetime | None]  # start of the last successful pass
    # Request waiting for the worker ("schedule" or "manual"): a single job per space
    # (`jobs`) runs them all, one after the other.
    requested: Mapped[str | None] = mapped_column(String(16))
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class RoutineRun(Base):
    """A routine pass. Kept if the routine is deleted (`routine_id` set to NULL, name copied):
    its cost still counts in the month's budget."""

    __tablename__ = "routine_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'done', 'failed')", name="status"),
        Index("ix_routine_runs_space_started", "space_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    routine_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routines.id", ondelete="SET NULL"), index=True
    )
    space_id: Mapped[UUID] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    trigger: Mapped[str] = mapped_column(String(16))  # schedule or manual
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default="running")
    model: Mapped[str] = mapped_column(String(200), default="")
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    summary: Mapped[str] = mapped_column(Text, default="")
    report: Mapped[str] = mapped_column(Text, default="")  # output text (page, email…)
    # What was output: {"page": path, "inbox": [titles], "emails": n, "webhook": bool}.
    outputs: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    commit: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cached_tokens: Mapped[int] = mapped_column(default=0)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))  # estimated €; None: subscription
    quota: Mapped[dict[str, object] | None] = mapped_column(JSONB)  # subscription (ai.quota)
    error: Mapped[str | None] = mapped_column(Text)


class SSOProvider(Base):
    """Login provider (OIDC or GitHub), configured by the super admin."""

    __tablename__ = "sso_providers"
    __table_args__ = (
        CheckConstraint("kind IN ('oidc', 'gitlab', 'google', 'github')", name="kind"),
    )

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(120))  # button label
    issuer: Mapped[str] = mapped_column(Text, default="")  # OIDC: issuer URL
    client_id: Mapped[str] = mapped_column(Text)
    client_secret: Mapped[bytes] = mapped_column(LargeBinary)  # encrypted (SecretBox)
    # Create an account on the first login of an unknown address. Disabled by default:
    # with GitHub or Google, anyone on the Internet would otherwise get an account.
    auto_create: Mapped[bool] = mapped_column(Boolean, default=False)
    # Email domains allowed for automatic creation and linking; empty: no filter.
    allowed_domains: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # Link a first login to the account with the same address (verified on both sides).
    # False: linking is done from "My account", while logged in.
    link_by_email: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SSOState(Base):
    """SSO login in progress (10 minutes): protects against forgery of the callback."""

    __tablename__ = "sso_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(ForeignKey("sso_providers.slug", ondelete="CASCADE"))
    verifier: Mapped[str] = mapped_column(String(128))  # PKCE
    nonce: Mapped[str] = mapped_column(String(64))
    next_url: Mapped[str] = mapped_column(Text, default="/")
    # Linking requested from "My account": the logged-in account that receives the identity.
    link_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime]


class UserIdentity(Base):
    """External identity attached to an account."""

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(ForeignKey("sso_providers.slug", ondelete="CASCADE"))
    subject: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None]


class Webhook(Base):
    """Outgoing webhook of an organization: HMAC-SHA256-signed events."""

    __tablename__ = "webhooks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text)
    secret: Mapped[bytes] = mapped_column(LargeBinary)  # encrypted (SecretBox)
    events: Mapped[list[str]] = mapped_column(JSONB, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_delivery_at: Mapped[datetime | None]
    last_status: Mapped[str | None] = mapped_column(String(200))
    # Circuit breaker: consecutive delivery failures, and since when (reset on the first
    # success). Beyond the threshold, `active` turns false until an admin re-enables it.
    failures: Mapped[int] = mapped_column(default=0, server_default="0")
    failing_since: Mapped[datetime | None]


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64))
    org_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    space_id: Mapped[UUID | None] = mapped_column(ForeignKey("spaces.id", ondelete="SET NULL"))
    details: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)


class MailSettings(Base):
    """Instance email sending (SMTP), configured by the super admin. A single row."""

    __tablename__ = "mail_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(default=587)
    security: Mapped[str] = mapped_column(String(16), default="starttls")  # starttls, ssl, none
    username: Mapped[str] = mapped_column(String(255), default="")
    password: Mapped[bytes | None] = mapped_column(LargeBinary)  # encrypted (SecretBox)
    sender: Mapped[str] = mapped_column(String(320))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("security IN ('starttls', 'ssl', 'none')", name="security"),
    )


class Invitation(Base):
    """Invitation to join an organization (and spaces), via a single-use link."""

    __tablename__ = "invitations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    org_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    org_role: Mapped[str] = mapped_column(String(16), default="member")
    space_roles: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)  # id → role
    invited_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    accepted_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class PasswordReset(Base):
    """Forgot-password link: single use, 30 minutes."""

    __tablename__ = "password_resets"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class EmailCheck(Base):
    """Email address verification link: single use, 3 days."""

    __tablename__ = "email_checks"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class ChangeMark(Base):
    """Tidy-up decision on a page changed outside the tidy-up (docs/concepts.md, Tidy-up).

    `until`: commit up to which the decision holds; a more recent change to the page brings
    it back into the inbox. `pending`: not a decision, but a pass interrupted before reaching
    one; the page remains to be examined and `info["attempts"]` counts the passes."""

    __tablename__ = "change_marks"

    space_id: Mapped[UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(Text, primary_key=True)  # relative to content/
    until: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(8))  # done, left or pending
    reason: Mapped[str] = mapped_column(Text, default="")
    info: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (CheckConstraint("status IN ('done', 'left', 'pending')", name="status"),)


class PageReport(Base):
    """A page reported as outdated by someone who reads it (`services.reports`): readers
    cannot write to the page, so the report lives here. Open until the page is verified,
    the report dismissed by a contributor, or withdrawn by its author."""

    __tablename__ = "page_reports"
    __table_args__ = (
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('verified', 'dismissed', 'withdrawn')",
            name="resolution",
        ),
        Index(
            "ix_page_reports_open", "space_id", "path", postgresql_where=text("resolved_at IS NULL")
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)  # relative to content/
    reported_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolution: Mapped[str | None] = mapped_column(String(16))
