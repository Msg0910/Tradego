"""
Tradego API Data Transfer Objects & Schema Contracts.
Defines typed request/response contracts for authentication, commands, health,
read-only snapshots, and structured error envelopes.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from ..contracts import CommandStatus, CommandType


class LoginRequest(BaseModel):
    """Operator native credential payload."""
    username: str = Field(..., min_length=1, description="Operator username / ID")
    password: str = Field(..., min_length=1, description="Operator password")


class LoginResponse(BaseModel):
    """Successful native authentication session response."""
    session_token: str = Field(..., description="High-entropy opaque bearer token (tg_sess_...)")
    operator_id: str
    roles: List[str]
    capabilities: List[str]
    expires_at: str


class LogoutResponse(BaseModel):
    """Session revocation confirmation."""
    status: str = "LOGGED_OUT"
    message: str = "Session successfully invalidated"


class HealthResponse(BaseModel):
    """Platform liveness probe response."""
    status: str = "UP"
    timestamp: str
    version: str = "1.0.0"


class ReadinessResponse(BaseModel):
    """Platform readiness probe response checking engine and audit health."""
    status: str = "READY"
    guard_state: str
    audit_logger: str
    timestamp: str


class CommandApiRequest(BaseModel):
    """Ingress command payload submitted via HTTP POST."""
    command_type: CommandType
    parameters: Dict[str, Any] = Field(default_factory=dict)


class CommandApiResponse(BaseModel):
    """Authoritative result returned following command execution."""
    command_id: str
    status: CommandStatus
    guard_state: str
    sequence: int
    reason: str = ""
    timestamp: str


class SnapshotResponse(BaseModel):
    """
    Authoritative state projection anchored to the sequence frontier S_snap.
    Client stream reconcilers bootstrap baseline from this response.
    """
    snapshot_id: str
    snapshot_timestamp: str
    authoritative_sequence: int  # S_snap
    runtime_mode: str
    guard_state: str
    details: Dict[str, Any] = Field(default_factory=dict)


class ErrorDetail(BaseModel):
    """Canonical structured error details."""
    code: str
    message: str
    correlation_id: str
    details: Dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """Universal structured error response contract."""
    error: ErrorDetail


class RecoveryRequestPayload(BaseModel):
    """Payload to initiate a HALTED recovery challenge."""
    ttl_seconds: int = Field(default=300, ge=30, le=3600)


class RecoveryRequestResponse(BaseModel):
    """Challenge created by Operator A."""
    challenge_id: str
    operator_a_id: str
    confirmation_token: str
    token: Optional[str] = None
    expires_at: str
    status: str
    guard_state: str
    correlation_id: Optional[str] = None


class RecoveryConfirmPayload(BaseModel):
    """Confirmation submitted by distinct Operator B."""
    challenge_id: str
    confirmation_token: str


class RecoveryConfirmResponse(BaseModel):
    """Confirmation result following two-person recovery."""
    status: str
    recovered: bool = True
    guard_state: str
    operator_a_id: str
    operator_b_id: str
    timestamp: str
    correlation_id: Optional[str] = None


class RecoveryStatusResponse(BaseModel):
    """Current recovery challenge and guard status."""
    has_active_challenge: bool = False
    challenge: Optional[Dict[str, Any]] = None
    active_challenge: Optional[Dict[str, Any]] = None
    guard_state: str
    recovery_state: Optional[str] = "CLEAN"
    last_recovery_timestamp: Optional[str] = None
    in_flight_count: int = 0
    unknown_count: int = 0
    quarantined_count: int = 0
    reconciliation_required: bool = False
    wal_sequence: int = 0
    audit_chain_valid: bool = True
    quarantined_executions: List[Dict[str, Any]] = Field(default_factory=list)


class PortfolioResponse(BaseModel):
    """Authoritative read-only portfolio projection."""
    is_available: bool
    account_id: Optional[str] = None
    total_equity: Optional[float] = None
    cash: Optional[float] = None
    available_cash: Optional[float] = None
    peak_equity: Optional[float] = None
    realized_pnl: Optional[float] = None
    realized_pnl_today: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    unrealized_pnl_current: Optional[float] = None
    total_pnl_today: Optional[float] = None
    drawdown_pct: Optional[float] = None
    currency: Optional[str] = None
    open_positions_count: int = 0
    total_gross_exposure: Optional[float] = None
    total_net_exposure: Optional[float] = None
    positions: List[Dict[str, Any]] = Field(default_factory=list)
    note: Optional[str] = None


class RiskResponse(BaseModel):
    """Authoritative read-only risk state projection."""
    is_available: bool
    guard_state: str
    risk_limits_status: Optional[str] = None
    can_submit_speculative_entry: bool = False
    can_submit_exit: bool = True
    max_drawdown: Optional[float] = None
    max_capital: Optional[float] = None
    current_exposure: Optional[float] = None
    margin_available: Optional[float] = None
    max_gross_leverage: Optional[float] = None
    max_net_leverage: Optional[float] = None
    limits: Optional[Dict[str, Any]] = None
    note: Optional[str] = None


class CreateIntentPayload(BaseModel):
    """Payload for submitting a new execution intent."""
    symbol: str
    side: str
    quantity: int
    order_type: str = "LIMIT"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    reason: Optional[str] = ""
    ttl_seconds: Optional[int] = 3600
    initial_state: Optional[str] = "PENDING_APPROVAL"


class RejectIntentPayload(BaseModel):
    """Payload for rejecting an execution intent."""
    reason: str


class CancelIntentPayload(BaseModel):
    """Payload for cancelling an execution intent."""
    reason: str


class IntentResponse(BaseModel):
    """Schema for individual Execution Intent representation."""
    intent_id: str
    correlation_id: str
    creator_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    reason: Optional[str] = ""
    state: str
    approver_id: Optional[str] = None
    rejection_reason: Optional[str] = None
    cancellation_reason: Optional[str] = None
    created_at: str
    updated_at: str
    expires_at: str
    status_category: str = "ACCEPTED_AS_INTENT"
    is_executed: bool = False
    note: str = "INTENT ONLY — NOT EXECUTED"
    risk_status: Optional[str] = None
    risk_evaluator_id: Optional[str] = None
    risk_rejection_reason: Optional[str] = None
    risk_evaluated_at: Optional[str] = None
    risk_details: Optional[Dict[str, Any]] = None


class IntentListResponse(BaseModel):
    """Schema for listing execution intents."""
    intents: List[IntentResponse]
    count: int


class RiskEvaluationResponse(BaseModel):
    """Schema for pre-trade risk evaluation outcome."""
    intent_id: str
    decision: str
    reason: str
    details: Dict[str, Any] = Field(default_factory=dict)
    evaluator_id: str
    evaluated_at: str
    correlation_id: str
    status_category: str = "ACCEPTED_AS_INTENT"
    is_executed: bool = False
    resulting_state: str


class OrderInstructionResponse(BaseModel):
    """Schema for canonical OrderInstruction representation."""
    instruction_id: str
    intent_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    risk_evaluation_reference: str
    approval_reference: str
    correlation_id: str
    provenance: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
    state: str
    broker_order_id: Optional[str] = None
    dispatched_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    failure_reason: Optional[str] = None
    cancellation_reason: Optional[str] = None
    status_category: str = "ORDER_INSTRUCTION"
    is_executed: bool = False
    note: str = "ORDER INSTRUCTION ONLY — NOT CLAIMED AS EXECUTED"


class OrderInstructionListResponse(BaseModel):
    """Schema for listing canonical OrderInstructions."""
    instructions: List[OrderInstructionResponse]
    count: int


class CancelInstructionPayload(BaseModel):
    """Payload for cancelling an order instruction."""
    reason: str = "OPERATOR_CANCELLED"


class CreateInstructionPayload(BaseModel):
    """Payload for creating an order instruction from an intent."""
    intent_id: str


class FillModel(BaseModel):
    """Schema for individual venue fill record."""
    fill_id: str
    quantity: int
    price: Optional[float] = None
    timestamp: str
    execution_id: str = ""
    broker_fill_id: Optional[str] = None
    fee: float = 0.0


class ExecutionResponse(BaseModel):
    """Schema for authoritative order execution representation."""
    execution_id: str
    instruction_id: str
    intent_id: str
    symbol: str
    side: str
    ordered_quantity: int
    filled_quantity: int
    remaining_quantity: int
    limit_price: Optional[float] = None
    price: Optional[float] = None
    average_fill_price: Optional[float] = None
    broker_order_id: Optional[str] = None
    broker_timestamp: Optional[str] = None
    gateway_timestamp: str
    current_state: str
    reconciliation_status: str
    correlation_id: str
    rejection_reason: Optional[str] = None
    failure_reason: Optional[str] = None
    cancellation_reason: Optional[str] = None
    last_error: Optional[str] = None
    reconciliation_notes: Optional[str] = None
    reconciliation_details: Optional[str] = None
    fills_count: int = 0
    fills: List[FillModel] = Field(default_factory=list)
    last_update_timestamp: str = ""


class ExecutionListResponse(BaseModel):
    """Schema for listing execution records."""
    executions: List[ExecutionResponse]
    count: int


class BrokerStatusResponse(BaseModel):
    """Schema for broker connectivity & execution mode point-in-time status."""
    execution_mode: str
    connectivity_state: str
    active_broker: str
    is_authenticated: bool
    credentials: Dict[str, Any] = Field(default_factory=dict)
    risk_gate_available: bool
    guard_state: str
    last_heartbeat: Optional[str] = None
    last_broker_event: Optional[Dict[str, Any]] = None
    last_reconciliation: Optional[Dict[str, Any]] = None
    failure_reason: Optional[str] = None


class BrokerModeResponse(BaseModel):
    """Schema for execution mode response."""
    execution_mode: str
    is_live: bool
    guard_state: str
    connectivity_state: str
    message: str


class BrokerModeLivePayload(BaseModel):
    """Payload to request LIVE mode activation."""
    confirm_live: bool = Field(default=False, description="Explicit operator acknowledgement of live trading risk.")


class BrokerConnectPayload(BaseModel):
    """Optional payload when requesting broker connection."""
    venue_name: Optional[str] = None
    client_id: Optional[str] = None
    access_token: Optional[str] = None
    account_id: Optional[str] = None


class BrokerReconcileResponse(BaseModel):
    """Schema for broker book reconciliation report."""
    result: str
    timestamp: str
    inspected_orders_count: int
    matched_count: int
    mismatch_count: int
    unknown_count: int
    discrepancies: List[Dict[str, Any]] = Field(default_factory=list)
    operator_notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase 10 Production Operations & Disaster Recovery Models
# ---------------------------------------------------------------------------

class LivezResponse(BaseModel):
    """Process liveness contract."""
    status: str = "UP"
    timestamp: str
    version: str = "1.0.0"


class ReadyzResponse(BaseModel):
    """Operational readiness contract."""
    status: str
    reason: Optional[str] = None
    guard_state: str
    recovery_state: str
    timestamp: str


class DetailedHealthResponse(BaseModel):
    """Deep operational observability contract (secrets strictly redacted)."""
    status: str
    execution_mode: str
    trading_guard_state: str
    wal_health: Dict[str, Any] = Field(default_factory=dict)
    audit_chain_status: Dict[str, Any] = Field(default_factory=dict)
    broker_connectivity: Dict[str, Any] = Field(default_factory=dict)
    disaster_recovery: Dict[str, Any] = Field(default_factory=dict)
    disk_health: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str


class ReconciliationActionPayload(BaseModel):
    """Operator request to reconcile quarantined or mismatch execution."""
    execution_id: str
    action: str = Field(..., description="Action: CONFIRM_FILLED, CONFIRM_CANCELLED, MANUAL_RECONCILED")
    operator_notes: str = Field(..., min_length=1, description="Audit justification for reconciliation action")


class UnquarantinePayload(BaseModel):
    """Operator request to lift quarantine on an execution."""
    execution_id: str
    confirmation_reason: str = Field(..., min_length=1, description="Audit rationale for safe unquarantine")
    second_operator_id: Optional[str] = Field(None, description="Second authorizer for two-person authorization")


class VenueResponse(BaseModel):
    """Isolated multi-venue representation without credential leakage."""
    venue_id: str
    broker_name: str
    operational_status: str
    connectivity_state: str
    reconciliation_status: str
    supported_exchanges: List[str]
    supported_asset_classes: List[str]
    last_heartbeat: Optional[str] = None
    has_credentials: bool = False


class VenueListResponse(BaseModel):
    """List of configured multi-venue adapters."""
    venues: List[VenueResponse]
    total_venues: int




