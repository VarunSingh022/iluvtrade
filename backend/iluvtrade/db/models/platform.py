"""Platform-core tables: identity, tenancy, authorization, audit, notifications."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from iluvtrade.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin, UtcDateTime


class Role(enum.StrEnum):
    """What a member may do inside one organization.

    Deliberately a short, total ordering rather than a permission matrix: the
    product has three real audiences (someone who looks, someone who builds and
    trades, someone who administers), and a matrix nobody can enumerate is how
    authorization bugs hide.
    """

    VIEWER = "viewer"
    TRADER = "trader"
    ADMIN = "admin"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return {"viewer": 0, "trader": 1, "admin": 2, "owner": 3}[self.value]

    def satisfies(self, required: Role) -> bool:
        """Whether holding this role is enough to act as ``required``."""

        return self.rank >= required.rank


class UserStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class SubscriptionStatus(enum.StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"


class Organization(Base, IdMixin, TimestampMixin):
    """A tenant. Every piece of user data hangs off exactly one of these."""

    __tablename__ = "organizations"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Set when the org was created as the personal workspace of one user.
    personal_for_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class User(Base, IdMixin, TimestampMixin):
    """A person. Users are global; their *data* is never global."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    #: Argon2id. The plaintext never leaves the request that carried it.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, native_enum=False), default=UserStatus.ACTIVE, nullable=False
    )
    #: The user-level half of the live-trading gate. The deployment-level half is
    #: ``Settings.live_trading_enabled`` and the per-session half is an explicit
    #: confirmation; all three must hold. See :mod:`iluvtrade.trading.live`.
    live_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # --- two-factor authentication ----------------------------------------
    #: The TOTP shared secret, encrypted at rest with the same envelope as a
    #: broker credential — it is a credential, and storing it in the clear would
    #: let a database disclosure mint valid codes forever.
    mfa_secret: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: Set only once a code has been verified. Enrolment that flipped this on
    #: issue would lock out a user whose authenticator never got the secret.
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: Argon2 hashes of single-use recovery codes, one JSON array. Hashed for
    #: the same reason passwords are: they are password-equivalent.
    mfa_recovery_hashes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: The last TOTP counter accepted, so a code cannot be replayed inside its
    #: own validity window.
    mfa_last_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Membership(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The user↔organization edge, carrying the role.

    Carries :class:`OrgScopedMixin` rather than declaring the column itself, so
    that listing a workspace's members goes through
    :func:`~iluvtrade.platform.tenancy.scoped` like every other tenant read.
    The column is identical either way; what changes is that the tenancy guard
    now covers this table instead of refusing to.

    Queries that deliberately span tenants — "which workspaces does this user
    belong to?", asked at login — select directly and say so at the call site.
    """

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_membership"),)

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False), nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class Session(Base, IdMixin, TimestampMixin):
    """A logged-in session.

    The token itself is never stored: the row holds an HMAC of it, so a database
    disclosure does not hand an attacker working sessions.
    """

    __tablename__ = "sessions"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The organization this session is acting in. A user with several
    #: memberships holds one session per active organization, so authorization
    #: never has to guess which tenant a request meant.
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)


class AuditEvent(Base, IdMixin, OrgScopedMixin):
    """An append-only, tamper-evident record of something that happened.

    Written by :func:`iluvtrade.platform.audit.record` and never updated or
    deleted by application code. ``payload`` is redacted at the writer, not here:
    nothing that could carry a credential is handed to this table.

    **The chain.** Each event carries ``sequence`` (per organization, starting
    at 1), ``previous_hash`` and ``event_hash``, where the hash covers the
    event's own content *and* its predecessor's hash. Altering a payload,
    deleting an event or reordering two of them all break verification, because
    every later hash depends on every earlier one.

    The chain is **per organization**, not global. A global chain would mean one
    tenant's writes interleave into another's verification, so verifying tenant
    A would require reading tenant B's events — which is exactly the coupling
    the rest of the schema is built to avoid.

    What this is not: proof against an attacker who can rewrite the whole table.
    Someone with write access can recompute the entire chain. It detects
    *tampering with individual rows*, which is the realistic case — a bad
    ``UPDATE``, a partial restore, a deletion to hide one action. See
    ``docs/SECURITY.md``.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_org_created", "organization_id", "created_at"),
        # The chain is read in sequence order constantly; and the uniqueness is
        # what makes "no two events claim the same position" a database fact
        # rather than something the writer is trusted to maintain.
        UniqueConstraint("organization_id", "sequence", name="uq_audit_sequence"),
        Index("ix_audit_org_sequence", "organization_id", "sequence"),
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    #: Position in this organization's chain, from 1.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The predecessor's ``event_hash``; the genesis constant for the first.
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 over the canonical serialization of this event plus
    #: ``previous_hash``.
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class NotificationSeverity(enum.StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Notification(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """An operational message addressed to a user or to a whole organization."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notification_org_created", "organization_id", "created_at"),)

    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    severity: Mapped[NotificationSeverity] = mapped_column(
        Enum(NotificationSeverity, native_enum=False), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Subscription(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The organization's platform plan.

    Deliberately thin. PHASE 2 is explicit that elaborate billing must not
    precede correct security boundaries, so this records plan state and nothing
    a payment processor would own.
    """

    __tablename__ = "subscriptions"

    plan: Mapped[str] = mapped_column(String(64), nullable=False, default="free")
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, native_enum=False),
        default=SubscriptionStatus.TRIALING,
        nullable=False,
    )
    seats: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_period_end: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class PasswordResetToken(Base, IdMixin, TimestampMixin):
    """A single-use, short-lived permission to set one account's password.

    **Not organization-scoped, deliberately.** A user may belong to several
    organizations, and their password belongs to none of them — scoping this
    would force a choice of tenant that has no meaning and would let a member of
    one workspace reason about an account in another.

    The token is stored as an HMAC under the application secret, exactly as a
    session token is: a database disclosure yields no usable reset links. Only
    the hash is ever written, and the plaintext exists once, in the response
    that created it.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: Set the moment the token is used. Single-use is enforced on this column,
    #: not by deleting the row, so a replay is *distinguishable* from a token
    #: that never existed — which is what makes the audit trail readable.
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Invitation(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """An offer of membership in one organization, at one role.

    The token is hashed like every other credential here. It is returned to the
    **inviter** once, who passes it on however they like — this deployment has
    no email channel, and inventing one would be a lie rather than a feature.

    ``role`` is the role the invitation grants and cannot be changed by the
    person accepting it; ``email`` is the address that may accept, checked
    against the accepting user's own. Both together are what stops a leaked
    token from being a membership: the holder must also control that address.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        Index("ix_invitation_org_email", "organization_id", "email"),
        UniqueConstraint("token_hash", name="uq_invitation_token"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False), nullable=False)
    invited_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    accepted_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
