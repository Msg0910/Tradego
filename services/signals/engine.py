"""
Tradego Central Signal Engine (Phase 5).

Coordinates strategy registration, context construction from Phase 2/3/4 state,
synchronous strategy evaluation, deterministic arbitration, and signal dispatching.
"""

from abc import ABC, abstractmethod
import threading
from datetime import datetime
from typing import Callable, Dict, List, Optional

from services.analytics.engine import FeatureEngine
from services.candles.engine import CandleEngine
from services.candles.models import Candle
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import InstrumentId, InstrumentRegistry
from services.market_state.state import InstrumentStateSnapshot
from services.market_state.store import InstrumentStateStore
from .base import BaseStrategy
from .context import StrategyContext
from .models import DecisionType, PositionView, SignalCandidate, TriggerMode
from .scoring.arbiter import DeterministicSignalArbiter


class SignalDispatcher(ABC):
    """Abstract signal dispatch interface."""

    @abstractmethod
    def dispatch(self, signal: SignalCandidate) -> None:
        """Dispatches a confirmed or candidate signal to downstream consumers."""
        ...


class SyncSignalDispatcher(SignalDispatcher):
    """
    Synchronous in-memory signal dispatcher with exception isolation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: List[Callable[[SignalCandidate], None]] = []

    def add_listener(self, callback: Callable[[SignalCandidate], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[SignalCandidate], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def dispatch(self, signal: SignalCandidate) -> None:
        with self._lock:
            listeners = list(self._listeners)

        for listener in listeners:
            try:
                listener(signal)
            except Exception:
                pass  # Isolate listener failure to protect pipeline


class SignalEngine:
    """
    Central coordinator managing strategy evaluations and signal arbitration.
    """

    def __init__(
        self,
        registry: InstrumentRegistry,
        feature_engine: FeatureEngine,
        candle_engine: Optional[CandleEngine] = None,
        state_store: Optional[InstrumentStateStore] = None,
        arbiter: Optional[DeterministicSignalArbiter] = None,
        dispatcher: Optional[SignalDispatcher] = None,
    ) -> None:
        self._registry = registry
        self._feature_engine = feature_engine
        self._candle_engine = candle_engine
        self._state_store = state_store
        self._arbiter = arbiter or DeterministicSignalArbiter()
        self._dispatcher = dispatcher or SyncSignalDispatcher()

        # Strategy registry: InstrumentId -> List[BaseStrategy]
        self._strat_lock = threading.Lock()
        self._strategies: Dict[InstrumentId, List[BaseStrategy]] = {}

        # Optional read-only position provider callback: InstrumentId -> Optional[PositionView]
        self._position_provider: Optional[Callable[[InstrumentId], Optional[PositionView]]] = None

        # Auto-attach to CandleEngine if provided
        if self._candle_engine is not None:
            self._candle_engine.add_candle_listener(self.on_candle_closed)

    def set_position_provider(
        self, provider: Callable[[InstrumentId], Optional[PositionView]]
    ) -> None:
        """Sets a read-only callback to query current PositionView per instrument."""
        self._position_provider = provider

    def register_strategy(self, instrument_id: InstrumentId, strategy: BaseStrategy) -> None:
        """Registers a strategy for an instrument."""
        with self._strat_lock:
            strats = self._strategies.setdefault(instrument_id, [])
            if strategy not in strats:
                strats.append(strategy)

    def add_signal_listener(self, callback: Callable[[SignalCandidate], None]) -> None:
        """Adds downstream signal listener to the dispatcher."""
        if isinstance(self._dispatcher, SyncSignalDispatcher):
            self._dispatcher.add_listener(callback)

    def remove_signal_listener(self, callback: Callable[[SignalCandidate], None]) -> None:
        """Removes downstream signal listener from the dispatcher."""
        if isinstance(self._dispatcher, SyncSignalDispatcher):
            self._dispatcher.remove_listener(callback)

    def build_context(
        self,
        instrument_id: InstrumentId,
        trigger_mode: TriggerMode,
        evaluation_timestamp: datetime,
        candle: Optional[Candle] = None,
        position: Optional[PositionView] = None,
    ) -> Optional[StrategyContext]:
        """
        Builds an immutable StrategyContext for an instrument.
        """
        # 1. Phase 4 Features
        feature_snap = self._feature_engine.get_features(instrument_id)
        if feature_snap is None:
            return None

        # 2. Phase 2 Market State
        if self._state_store is not None:
            state_snap = self._state_store.get_snapshot(instrument_id)
        else:
            state_snap = None

        if state_snap is None:
            # Construct minimal empty state if not directly wired
            from services.market_state.state import InstrumentState
            inst_state = InstrumentState(
                instrument_id=instrument_id,
                provider="INTERNAL",
                provider_symbol_id=instrument_id.symbol,
                is_resolved=True,
            )
            state_snap = inst_state.create_snapshot()

        # 3. Phase 3 Active Previews if INTRABAR_PREVIEW
        preview_features = None
        if trigger_mode == TriggerMode.INTRABAR_PREVIEW and candle is not None:
            preview_features = self._feature_engine.get_active_preview(
                instrument_id, candle.timeframe
            )

        # 4. Position View
        pos_view = position
        if pos_view is None and self._position_provider is not None:
            pos_view = self._position_provider(instrument_id)

        return StrategyContext(
            instrument_id=instrument_id,
            trigger_mode=trigger_mode,
            market_state=state_snap,
            features=feature_snap,
            evaluation_timestamp=evaluation_timestamp,
            active_preview_features=preview_features,
            active_candle=candle,
            position=pos_view,
        )

    def evaluate_instrument(
        self,
        instrument_id: InstrumentId,
        trigger_mode: TriggerMode,
        evaluation_timestamp: datetime,
        candle: Optional[Candle] = None,
        position: Optional[PositionView] = None,
    ) -> List[SignalCandidate]:
        """
        Evaluates registered strategies for an instrument in a specific trigger mode.
        """
        with self._strat_lock:
            strats = list(self._strategies.get(instrument_id, []))

        if not strats:
            return []

        # Filter strategies matching the trigger mode
        matching_strats = [s for s in strats if s.trigger_mode == trigger_mode]
        if not matching_strats:
            return []

        context = self.build_context(
            instrument_id=instrument_id,
            trigger_mode=trigger_mode,
            evaluation_timestamp=evaluation_timestamp,
            candle=candle,
            position=position,
        )
        if context is None:
            return []

        candidates: List[SignalCandidate] = []
        for strat in matching_strats:
            decision = strat.evaluate(context)
            if decision.decision == DecisionType.TRADE and decision.candidate is not None:
                candidates.append(decision.candidate)

        # Arbitrate and rank deterministically
        ranked = self._arbiter.arbitrate(candidates)

        # Dispatch
        for sig in ranked:
            self._dispatcher.dispatch(sig)

        return ranked

    def on_candle_closed(self, candle: Candle) -> List[SignalCandidate]:
        """
        Warm-path callback registered with CandleEngine.
        Triggered when a candle officially closes (TriggerMode.BAR_CLOSE).
        """
        if not candle.is_closed:
            return []

        return self.evaluate_instrument(
            instrument_id=candle.instrument_id,
            trigger_mode=TriggerMode.BAR_CLOSE,
            evaluation_timestamp=candle.end_time,
            candle=candle,
        )

    def on_state_changed(
        self,
        snapshot: InstrumentStateSnapshot,
        event: Optional[MarketEvent] = None,
    ) -> List[SignalCandidate]:
        """
        Hot-path callback triggered on state updates (TriggerMode.INTRABAR_PREVIEW).
        """
        if snapshot.instrument_id is None:
            return []

        eval_ts = (
            snapshot.last_exchange_timestamp
            or snapshot.last_provider_timestamp
            or datetime.now()
        )

        return self.evaluate_instrument(
            instrument_id=snapshot.instrument_id,
            trigger_mode=TriggerMode.INTRABAR_PREVIEW,
            evaluation_timestamp=eval_ts,
        )
