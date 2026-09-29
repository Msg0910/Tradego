"""
Tradego Phase 10 — Multi-Venue Registry & Deterministic Router.

Provides isolated multi-venue execution readiness:
- BrokerVenue: Encapsulates isolated venue adapter, credentials reference, and status.
- BrokerVenueRegistry: Registration, retrieval, and duplication rejection.
- VenueRoutingPolicy: Deterministic mapping from asset/exchange/symbol-prefix to venue.
- MultiVenueRouter: Routing enforcement with strict ROUTING_UNAVAILABLE fail-closed behavior.
- Zero credential duplication into routing payloads.
- Strictly NO automatic failover to prevent duplicate order generation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from .broker_adapter import BrokerAdapter
from .broker_credentials import BrokerCredentialsConfig
from .order_instruction import OrderInstruction


@dataclass
class BrokerVenue:
    """
    Isolated container for a registered broker venue.
    Maintains venue-specific adapter, credentials reference, and operational status.
    """
    venue_id: str
    adapter: BrokerAdapter
    credentials: Optional[BrokerCredentialsConfig] = None
    connectivity_state: str = "DISCONNECTED"
    last_heartbeat: Optional[str] = None
    reconciliation_status: str = "UNKNOWN"
    supported_exchanges: List[str] = field(default_factory=list)
    supported_asset_classes: List[str] = field(default_factory=list)
    operational_status: str = "ACTIVE"  # "ACTIVE", "PAUSED", "DRAINING", "DISABLED"

    def to_dict(self) -> Dict[str, Any]:
        """Returns safe representation without credential leakage."""
        creds_summary = self.credentials.redacted_dict() if self.credentials else None
        return {
            "venue_id": self.venue_id,
            "connectivity_state": self.connectivity_state,
            "last_heartbeat": self.last_heartbeat,
            "reconciliation_status": self.reconciliation_status,
            "supported_exchanges": list(self.supported_exchanges),
            "supported_asset_classes": list(self.supported_asset_classes),
            "operational_status": self.operational_status,
            "has_credentials": bool(self.credentials),
            "credentials": creds_summary,
        }


class BrokerVenueRegistry:
    """
    Thread-safe registry for broker venues.
    Enforces venue uniqueness, rejects duplicates, and manages isolated venue instances.
    """

    def __init__(self, audit_logger: Optional[Any] = None, **kwargs: Any) -> None:
        self._lock = threading.RLock()
        self._audit = audit_logger
        self._venues: Dict[str, BrokerVenue] = {}

    def register_venue(self, venue: BrokerVenue) -> None:
        """
        Registers a broker venue.
        Rejects duplicate venue IDs to guarantee unambiguous routing.
        """
        with self._lock:
            vid = venue.venue_id.strip()
            if not vid:
                raise ValueError("INVALID_VENUE_ID: venue_id must be non-empty string.")
            if vid in self._venues:
                raise ValueError(f"DUPLICATE_VENUE_REJECTED: Venue '{vid}' is already registered.")
            self._venues[vid] = venue

    def unregister_venue(self, venue_id: str) -> bool:
        with self._lock:
            if venue_id in self._venues:
                del self._venues[venue_id]
                return True
            return False

    def get_venue(self, venue_id: str) -> Optional[BrokerVenue]:
        with self._lock:
            return self._venues.get(venue_id)

    def list_venues(self) -> List[BrokerVenue]:
        with self._lock:
            return list(self._venues.values())

    def update_heartbeat(self, venue_id: str, timestamp: Optional[str] = None) -> None:
        with self._lock:
            venue = self._venues.get(venue_id)
            if venue:
                venue.last_heartbeat = timestamp or datetime.now(timezone.utc).isoformat()
                venue.connectivity_state = "CONNECTED"

    def set_connectivity_state(self, venue_id: str, state: str) -> None:
        with self._lock:
            venue = self._venues.get(venue_id)
            if venue:
                venue.connectivity_state = state

    def get_all_venue_statuses(self) -> Dict[str, Any]:
        with self._lock:
            return {vid: v.to_dict() for vid, v in self._venues.items()}


class VenueRoutingPolicy:
    """
    Deterministic configuration mapping order characteristics to a target venue_id.
    Never invents routes: unconfigured routes fail closed.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Mapping: (exchange, asset_class) -> venue_id
        self._exchange_asset_routes: Dict[Tuple[str, str], str] = {}
        # Mapping: symbol_prefix -> venue_id
        self._prefix_routes: Dict[str, str] = {}
        # Explicit default fallback venue (only if configured)
        self._default_venue: Optional[str] = None

    def add_route(self, exchange: str, asset_class: str, venue_id: str) -> None:
        with self._lock:
            key = (exchange.upper().strip(), asset_class.upper().strip())
            self._exchange_asset_routes[key] = venue_id

    def add_prefix_route(self, prefix: str, venue_id: str) -> None:
        with self._lock:
            self._prefix_routes[prefix.upper().strip()] = venue_id

    def set_default_venue(self, venue_id: Optional[str]) -> None:
        with self._lock:
            self._default_venue = venue_id

    def resolve(self, symbol: str, exchange: Optional[str] = None, asset_class: Optional[str] = None) -> Optional[str]:
        """
        Deterministically resolves target venue ID. Returns None if unconfigured.
        """
        with self._lock:
            # 1. Check exchange + asset class route
            if exchange and asset_class:
                key = (exchange.upper().strip(), asset_class.upper().strip())
                if key in self._exchange_asset_routes:
                    return self._exchange_asset_routes[key]

            # 2. Check symbol prefix route
            sym_clean = symbol.upper().strip()
            for prefix, vid in self._prefix_routes.items():
                if sym_clean.startswith(prefix):
                    return vid

            # 3. Explicit default venue fallback
            return self._default_venue


class MultiVenueRouter:
    """
    Authoritative order instruction router across registered venues.
    Guarantees:
    - Deterministic routing based on explicit VenueRoutingPolicy
    - Unconfigured routes fail closed with ROUTING_UNAVAILABLE
    - No automatic failover that could generate duplicate orders
    - Generates venue-isolated idempotency key: TG-DISPATCH-{venue_id}-{instruction_id}
    """

    def __init__(
        self,
        registry: BrokerVenueRegistry,
        policy: Optional[VenueRoutingPolicy] = None,
        audit_logger: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        self._registry = registry
        self._policy = policy or VenueRoutingPolicy()
        self._audit = audit_logger
        self._lock = threading.RLock()

    def route(self, instruction: OrderInstruction) -> Tuple[BrokerVenue, str]:
        """
        Resolves destination venue and creates venue-isolated idempotency key.
        Fails closed with ValueError('ROUTING_UNAVAILABLE') if no route is configured.
        """
        with self._lock:
            # Extract exchange / asset_class from provenance if available
            prov = getattr(instruction, "provenance", {}) or {}
            exchange = prov.get("exchange")
            asset_class = prov.get("asset_class")

            target_venue_id = self._policy.resolve(
                symbol=instruction.symbol,
                exchange=exchange,
                asset_class=asset_class,
            )

            if not target_venue_id:
                raise ValueError(
                    f"ROUTING_UNAVAILABLE: No configured routing policy for symbol '{instruction.symbol}'."
                )

            venue = self._registry.get_venue(target_venue_id)
            if not venue:
                raise ValueError(
                    f"ROUTING_UNAVAILABLE: Target venue '{target_venue_id}' is not registered."
                )

            if venue.operational_status != "ACTIVE":
                raise ValueError(
                    f"ROUTING_UNAVAILABLE: Target venue '{target_venue_id}' is {venue.operational_status}."
                )

            # Generate venue-isolated idempotency key
            idempotency_key = f"TG-DISPATCH-{venue.venue_id}-{instruction.instruction_id}"
            return venue, idempotency_key


# Alias for backward-compatibility
MultiVenueBrokerRegistry = BrokerVenueRegistry
