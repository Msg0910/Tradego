"""
Tradego API & Event Boundary Gateway (Presentation / Boundary Layer).
Insulates the Phase 1–8 in-memory trading core, exports state projections,
and provides authenticated, capability-verified command dispatch.
"""

from .contracts import (
    Capability,
    CommandRequest,
    CommandResult,
    CommandStatus,
    CommandType,
    SnapshotPayload,
    TradegoEventEnvelope,
)
from .security import (
    CapabilityChecker,
    InMemorySessionStore,
    NativeCredentialStore,
    SessionContext,
    Tier1AuditLogger,
)
from .adapters import MarketStateAdapter, PortfolioRiskAdapter
from .broadcaster import EventBroadcaster, SequenceManager
from .command import CommandGateway
from .projection import ClientProjection, ProjectionState, SnapshotGenerator
from .recovery import RecoveryChallenge, RecoveryManager
from .intent import ExecutionIntent, ExecutionIntentManager, IntentState
from .risk_gate import PreTradeRiskEvaluator, RiskDecisionType, RiskEvaluationResult, RiskGate
from .broker_adapter import (
    BrokerAdapter,
    BrokerDispatchResult,
    LiveBrokerAdapter,
    MockLiveBrokerAdapter,
    PaperBrokerAdapter,
)
from .order_instruction import InstructionState, OrderInstruction, OrderInstructionManager
from .execution import (
    ExecutionRecord,
    ExecutionReconciliationEngine,
    ExecutionState,
    ExecutionStateManager,
    FillRecord,
    ReconciliationStatus,
)
from .broker_credentials import BrokerCredentialsConfig
from .broker_connectivity import (
    BrokerConnectivityManager,
    BrokerConnectivityState,
    BrokerReconciliationResult,
    ExecutionMode,
    ReconciliationReport,
)
from .persistence import (
    DurableEvent,
    PersistenceManager,
    WALIntegrityResult,
    WALReader,
    WALWriter,
)
from .disaster_recovery import (
    DisasterRecoveryManager,
    QuarantineRecord,
    RecoveryRecord,
    RecoveryState,
)
from .multi_venue import (
    BrokerVenue,
    BrokerVenueRegistry,
    MultiVenueRouter,
    VenueRoutingPolicy,
)
from .observability import (
    HealthCheckResult,
    HealthStatus,
    ObservabilityManager,
    SystemHealthMonitor,
)
from .security import (
    AuditChainVerificationResult,
    verify_audit_chain,
)

__all__ = [
    "TradegoEventEnvelope",
    "CommandType",
    "Capability",
    "CommandStatus",
    "CommandRequest",
    "CommandResult",
    "SnapshotPayload",
    "SessionContext",
    "InMemorySessionStore",
    "NativeCredentialStore",
    "CapabilityChecker",
    "Tier1AuditLogger",
    "SequenceManager",
    "EventBroadcaster",
    "CommandGateway",
    "MarketStateAdapter",
    "PortfolioRiskAdapter",
    "SnapshotGenerator",
    "ClientProjection",
    "ProjectionState",
    "RecoveryChallenge",
    "RecoveryManager",
    "ExecutionIntent",
    "ExecutionIntentManager",
    "IntentState",
    "PreTradeRiskEvaluator",
    "RiskGate",
    "RiskDecisionType",
    "RiskEvaluationResult",
    "BrokerAdapter",
    "BrokerDispatchResult",
    "PaperBrokerAdapter",
    "LiveBrokerAdapter",
    "MockLiveBrokerAdapter",
    "InstructionState",
    "OrderInstruction",
    "OrderInstructionManager",
    "ExecutionState",
    "ReconciliationStatus",
    "FillRecord",
    "ExecutionRecord",
    "ExecutionReconciliationEngine",
    "ExecutionStateManager",
    "BrokerCredentialsConfig",
    "ExecutionMode",
    "BrokerConnectivityState",
    "BrokerReconciliationResult",
    "ReconciliationReport",
    "BrokerConnectivityManager",
    "DurableEvent",
    "PersistenceManager",
    "WALWriter",
    "WALReader",
    "WALIntegrityResult",
    "DisasterRecoveryManager",
    "RecoveryState",
    "RecoveryRecord",
    "QuarantineRecord",
    "BrokerVenue",
    "BrokerVenueRegistry",
    "VenueRoutingPolicy",
    "MultiVenueRouter",
    "HealthStatus",
    "HealthCheckResult",
    "ObservabilityManager",
    "SystemHealthMonitor",
    "AuditChainVerificationResult",
    "verify_audit_chain",
]

