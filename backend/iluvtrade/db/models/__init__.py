"""Every table in the application, imported here so metadata is complete.

Alembic autogeneration and ``Base.metadata.create_all`` both need every model
imported before they run; doing it in one place means a new table is registered
by adding it to this list rather than by remembering to import it somewhere.
"""

from iluvtrade.db.base import Base
from iluvtrade.db.models.backtest import BacktestJob, BacktestRun, JobStatus
from iluvtrade.db.models.broker import (
    BrokerAccount,
    BrokerConnection,
    BrokerKind,
    ConnectionState,
)
from iluvtrade.db.models.data import (
    Dataset,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    SourceKind,
)
from iluvtrade.db.models.platform import (
    AuditEvent,
    Invitation,
    Membership,
    Notification,
    NotificationSeverity,
    Organization,
    PasswordResetToken,
    Role,
    Session,
    Subscription,
    SubscriptionStatus,
    User,
    UserStatus,
)
from iluvtrade.db.models.reddesk import (
    BillingCadence,
    CreatorPayout,
    Entitlement,
    EntitlementStatus,
    Listing,
    ListingStatus,
    ListingVersion,
    Purchase,
    PurchaseStatus,
    Review,
    VersionAccessPolicy,
)
from iluvtrade.db.models.strategy import (
    CertificationStatus,
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
    StrategyVisibility,
)
from iluvtrade.db.models.trading import (
    SessionEvent,
    SessionFill,
    SessionOrder,
    SessionPosition,
    SessionStatus,
    TradingMode,
    TradingSession,
)

__all__ = [
    "AuditEvent",
    "BacktestJob",
    "BacktestRun",
    "Base",
    "BillingCadence",
    "BrokerAccount",
    "BrokerConnection",
    "BrokerKind",
    "CertificationStatus",
    "ConnectionState",
    "CreatorPayout",
    "DataSource",
    "Dataset",
    "DatasetVersion",
    "DatasetVersionStatus",
    "Entitlement",
    "EntitlementStatus",
    "Invitation",
    "JobStatus",
    "Listing",
    "ListingStatus",
    "ListingVersion",
    "Membership",
    "Notification",
    "NotificationSeverity",
    "Organization",
    "PasswordResetToken",
    "Purchase",
    "PurchaseStatus",
    "Review",
    "Role",
    "Session",
    "SessionEvent",
    "SessionFill",
    "SessionOrder",
    "SessionPosition",
    "SessionStatus",
    "SourceKind",
    "Strategy",
    "StrategyVersion",
    "StrategyVersionStatus",
    "StrategyVisibility",
    "Subscription",
    "SubscriptionStatus",
    "TradingMode",
    "TradingSession",
    "User",
    "UserStatus",
    "VersionAccessPolicy",
]
