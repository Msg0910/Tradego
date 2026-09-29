"""
Tradego Trading Runtime Coordinator (Phase 8).

Wires the complete in-memory pipeline from MarketDataGateway through to PositionAccounting.
Coordinates synchronous single-dispatch event handling, lifecycle transitions,
strategy scheduling with monotonic watermarks, fail-closed risk gating,
paper execution, and end-to-end lineage telemetry.
"""

from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from services.candles.engine import CandleEngine
from services.candles.models import Candle
from services.execution.models import CanonicalOrderStatus, Fill, OrderType, TimeInForce
from services.execution.paper_adapter import PaperExecutionAdapter
from services.execution.router import ExecutionRouter
from services.market_gateway.gateway import MarketDataGateway
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import InstrumentId, InstrumentRegistry
from services.market_state.store import InstrumentStateSnapshot, InstrumentStateStore
from services.analytics.engine import FeatureEngine
from services.risk.engine import RiskEngine
from services.signals.engine import SignalEngine
from services.signals.models import SignalCandidate, TriggerMode

from .config import RuntimeConfig
from .execution_coordinator import ExecutionCoordinator
from .guards import TradingGuard
from .health import HealthMonitor
from .lifecycle import RuntimeLifecycleManager
from .models import GuardTripReason, RuntimeCorrelationRecord, RuntimeMode, RuntimeState
from .portfolio import PortfolioRuntimeState
from .scheduler import StrategyScheduler
from .signal_risk_coordinator import SignalRiskCoordinator
from .telemetry import TelemetryCollector

logger = logging.getLogger("runtime.coordinator")


class PipelineCoordinator:
    """
    Central orchestration engine for the Tradego Phase 8 paper trading runtime.
    """

    def __init__(
        self,
        config: RuntimeConfig,
        gateway: MarketDataGateway,
        state_store: InstrumentStateStore,
        candle_engine: CandleEngine,
        feature_engine: FeatureEngine,
        signal_engine: SignalEngine,
        risk_engine: RiskEngine,
        router: ExecutionRouter,
        paper_adapter: PaperExecutionAdapter,
        registry: InstrumentRegistry,
        initial_cash: float = 1_000_000.0,
    ) -> None:
        # Enforce fail-closed LIVE lockout
        if config.runtime_mode == RuntimeMode.LIVE:
            raise RuntimeError("LIVE trading mode is structurally disabled in Phase 8 v1.")

        self._config = config
        self._gateway = gateway
        self._state_store = state_store
        self._candle_engine = candle_engine
        self._feature_engine = feature_engine
        self._signal_engine = signal_engine
        self._risk_engine = risk_engine
        self._router = router
        self._paper_adapter = paper_adapter
        self._registry = registry

        # Phase 8 Internal Subsystems
        self._lifecycle = RuntimeLifecycleManager(initial_state=RuntimeState.STARTING)
        self._guard = TradingGuard()
        self._scheduler = StrategyScheduler(min_intrabar_interval_ms=config.min_intrabar_eval_interval_ms)
        self._portfolio_state = PortfolioRuntimeState(
            account_id=f"{config.runtime_mode.value}_ACCOUNT",
            initial_cash=initial_cash,
        )
        self._signal_risk = SignalRiskCoordinator(
            risk_engine=self._risk_engine,
            portfolio_state=self._portfolio_state,
            guard=self._guard,
            registry=self._registry,
        )
        self._execution_coord = ExecutionCoordinator(
            router=self._router,
            portfolio_state=self._portfolio_state,
            guard=self._guard,
        )
        self._telemetry = TelemetryCollector(capacity=config.telemetry_ring_buffer_size)
        self._health = HealthMonitor(
            guard=self._guard,
            max_exception_burst_count=config.max_exception_burst_count,
            feed_stagnation_timeout_ms=config.feed_stagnation_timeout_ms,
        )

        # In-flight correlation tracking: client_order_id -> dict
        self._correlation_lock = threading.Lock()
        self._in_flight_correlations: Dict[str, Dict[str, Any]] = {}
        self._pending_immediate_fills: Dict[str, List[Fill]] = {}

        # Wire inter-component callbacks
        self._wire_pipeline()

    def _wire_pipeline(self) -> None:
        """Wires in-memory callbacks across upstream and runtime components."""
        # Supply read-only PositionView to Phase 5 strategies
        self._signal_engine.set_position_provider(self._portfolio_state.get_position_view)

        # Wire gateway tick dispatch to our synchronous hot path
        self._gateway.add_listener(self.on_market_event)

        # Wire paper adapter fill callback to portfolio accounting & telemetry
        self._paper_adapter.register_fill_callback(self._on_paper_fill)

    # =========================================================================
    # LIFECYCLE MANAGEMENT
    # =========================================================================

    @property
    def lifecycle_state(self) -> RuntimeState:
        return self._lifecycle.state

    @property
    def guard(self) -> TradingGuard:
        return self._guard

    @property
    def portfolio_state(self) -> PortfolioRuntimeState:
        return self._portfolio_state

    @property
    def scheduler(self) -> StrategyScheduler:
        return self._scheduler

    @property
    def telemetry(self) -> TelemetryCollector:
        return self._telemetry

    @property
    def health(self) -> HealthMonitor:
        return self._health

    def start(self) -> None:
        """
        Executes verified deterministic startup sequence.
        Transitions STARTING -> READY -> RUNNING.
        """
        if self._config.runtime_mode == RuntimeMode.LIVE:
            self._lifecycle.transition_to(RuntimeState.FAILED)
            raise RuntimeError("LIVE trading mode is structurally disabled.")

        try:
            # Enable execution router
            self._router.is_accepting_orders = True

            # Transition to READY
            self._lifecycle.transition_to(RuntimeState.READY)

            # Connect market data gateway
            self._gateway.start()

            # Transition to RUNNING
            self._lifecycle.transition_to(RuntimeState.RUNNING)
            logger.info("[TradingRuntime] System online in RUNNING state.")
        except Exception as e:
            logger.critical(f"[TradingRuntime] Startup failed: {e}", exc_info=True)
            self._guard.trip(GuardTripReason.STARTUP_FAILURE, str(e))
            self._lifecycle.transition_to(RuntimeState.FAILED)
            raise

    def stop(self) -> None:
        """
        Executes graceful, fail-safe shutdown sequence.
        Cancels working orders, drains in-flight fills, stops feeds, transitions to STOPPED.
        """
        logger.info("[TradingRuntime] Initiating shutdown...")
        self._lifecycle.transition_to(RuntimeState.SHUTTING_DOWN)

        # 1. Stop accepting new orders
        self._router.is_accepting_orders = False

        # 2. Cancel all active orders in execution registry
        with self._router._registry._lock:
            active_ids = [
                cid
                for cid, state in self._router._registry._states_by_client_id.items()
                if not state.is_terminal
            ]

        for cid in active_ids:
            try:
                self._router.cancel(cid)
            except Exception as e:
                logger.error(f"[TradingRuntime] Error cancelling order {cid} during shutdown: {e}")

        # 3. Disconnect market data feeds
        try:
            self._gateway.stop()
        except Exception as e:
            logger.error(f"[TradingRuntime] Error stopping gateway: {e}")

        # 4. Transition to STOPPED
        self._lifecycle.transition_to(RuntimeState.STOPPED)
        logger.info("[TradingRuntime] System safely STOPPED.")

    def pause(self) -> None:
        """Administratively pauses trading."""
        self._guard.pause()
        if self._lifecycle.state == RuntimeState.RUNNING:
            self._lifecycle.transition_to(RuntimeState.PAUSED)

    def resume(self) -> None:
        """Resumes trading from PAUSED state."""
        self._guard.resume()
        if self._lifecycle.state in (RuntimeState.PAUSED, RuntimeState.READY):
            self._lifecycle.transition_to(RuntimeState.RUNNING)

    def recover_halted(self, confirmation_token: str) -> bool:
        """
        Manual recovery from HALTED state.
        Transitions HALTED -> READY. Requires subsequent resume() to enter RUNNING.
        """
        success = self._guard.recover(confirmation_token, self._config.recovery_token)
        if success:
            self._lifecycle.transition_to(RuntimeState.READY)
            logger.info("[TradingRuntime] Successfully recovered from HALTED. State is now READY.")
            return True
        return False

    # =========================================================================
    # SYNCHRONOUS HOT PATH INGESTION
    # =========================================================================

    def on_market_event(self, event: MarketEvent) -> None:
        """
        Synchronous single-dispatch execution hot path.
        Executed on provider transport thread. Zero disk/network/database I/O.
        """
        t1_ns = time.perf_counter_ns()
        self._health.record_tick()

        # Drop tick if system is terminal or starting
        if not self._lifecycle.is_active():
            return

        # 1. State Store L1/L2 Book Update (Per-instrument lock in Phase 2)
        try:
            self._state_store.on_market_event(event)
        except Exception as e:
            self._health.record_error("state_store", e)

        # 2. Update mark price in PortfolioRuntimeState & ensure Feature store
        iid = self._registry.resolve(event.provider, event.provider_symbol_id)
        if iid is not None:
            if hasattr(self._feature_engine, "get_or_create_store"):
                self._feature_engine.get_or_create_store(iid)
            if getattr(event, "ltp", None) is not None:
                try:
                    self._portfolio_state.update_mark_price(iid, event.ltp, event.exchange_timestamp)
                except Exception as e:
                    self._health.record_error("portfolio_state", e)

        # 3. Candle Engine Tick Update & Bar Detection
        closed_candles: List[Candle] = []
        try:
            closed_candles = self._candle_engine.on_market_event(event)
        except Exception as e:
            self._health.record_error("candle_engine", e)

        # 4. Strategy Scheduling & Evaluation
        # Path A: Closed Candle (TriggerMode.BAR_CLOSE)
        if closed_candles:
            for candle in closed_candles:
                try:
                    self._feature_engine.on_candle_closed(candle)
                except Exception as e:
                    self._health.record_error("feature_engine", e)

                self._process_bar_close(candle, t1_ns)

        # Path B: Intrabar Preview (TriggerMode.INTRABAR_PREVIEW)
        elif iid is not None:
            snap = self._state_store.get_snapshot(iid)
            if snap is not None:
                try:
                    self._feature_engine.on_state_changed(snap, event)
                except Exception as e:
                    self._health.record_error("feature_engine", e)

                self._process_intrabar_preview(snap, event, t1_ns)

        # 5. Paper Adapter Order Matching (Evaluates resting limit orders against tick)
        try:
            self._paper_adapter.on_market_event(event)
        except Exception as e:
            self._health.record_error("paper_adapter", e)

    def _process_bar_close(self, candle: Candle, t1_ns: int) -> None:
        """Processes finalized candle through BAR_CLOSE strategies with watermark dedup."""
        if not self._guard.can_evaluate_strategies():
            return

        iid = candle.instrument_id
        with self._signal_engine._strat_lock:
            strategies = list(self._signal_engine._strategies.get(iid, []))

        for strat in strategies:
            if strat.trigger_mode != TriggerMode.BAR_CLOSE:
                continue

            strat_id = strat.strategy_id
            # Authoritative monotonic evaluation watermark check
            if not self._scheduler.should_evaluate_bar_close(strat_id, iid, candle.timeframe, candle.end_time):
                continue

            # Evaluate strategy
            try:
                candidates = self._signal_engine.evaluate_instrument(
                    instrument_id=iid,
                    trigger_mode=TriggerMode.BAR_CLOSE,
                    evaluation_timestamp=candle.end_time,
                    candle=candle,
                )
                self._scheduler.advance_watermark(strat_id, iid, candle.timeframe, candle.end_time)
            except Exception as e:
                self._health.record_error(f"strategy.{strat_id}", e)
                self._scheduler.advance_watermark(strat_id, iid, candle.timeframe, candle.end_time)
                continue

            # Dispatch candidates through risk and execution
            for sig in candidates:
                self._process_signal(
                    sig, current_price=candle.close, t1_ns=t1_ns, current_time=candle.end_time
                )

    def _process_intrabar_preview(
        self, snap: InstrumentStateSnapshot, event: MarketEvent, t1_ns: int
    ) -> None:
        """Processes streaming state update through INTRABAR_PREVIEW strategies."""
        if not self._guard.can_evaluate_strategies():
            return

        iid = snap.instrument_id
        if iid is None:
            return

        with self._signal_engine._strat_lock:
            strategies = list(self._signal_engine._strategies.get(iid, []))

        now_ns = time.perf_counter_ns()
        eval_ts = snap.last_exchange_timestamp or event.exchange_timestamp

        for strat in strategies:
            if strat.trigger_mode != TriggerMode.INTRABAR_PREVIEW:
                continue

            strat_id = strat.strategy_id
            # Frequency rate limit check (100 ms)
            if not self._scheduler.should_evaluate_intrabar(strat_id, iid, now_ns):
                continue

            try:
                self._scheduler.record_intrabar_eval(strat_id, iid, now_ns)
                candidates = self._signal_engine.evaluate_instrument(
                    instrument_id=iid,
                    trigger_mode=TriggerMode.INTRABAR_PREVIEW,
                    evaluation_timestamp=eval_ts,
                )
            except Exception as e:
                self._health.record_error(f"strategy.{strat_id}", e)
                continue

            current_price = snap.last_price or getattr(event, "ltp", None) or 0.0
            for sig in candidates:
                self._process_signal(
                    sig, current_price=current_price, t1_ns=t1_ns, current_time=eval_ts
                )

    def _process_signal(
        self,
        signal: SignalCandidate,
        current_price: float,
        t1_ns: int,
        current_time: Optional[datetime] = None,
    ) -> None:
        """
        Coordinates signal through risk evaluation and execution planning.
        """
        t2_ns = time.perf_counter_ns()

        eval_time = current_time or signal.market_timestamp or signal.generated_timestamp

        # Risk Admission Gate
        t3_ns = time.perf_counter_ns()
        intent = self._signal_risk.evaluate_signal(
            signal, current_price=current_price, eval_time=eval_time
        )
        t4_ns = time.perf_counter_ns()

        if intent is None:
            return

        # Execution Planning & Submission
        t5_ns = time.perf_counter_ns()
        order_type = OrderType.LIMIT if signal.suggested_entry_price is not None else OrderType.MARKET
        limit_price = signal.suggested_entry_price

        t6_ns = time.perf_counter_ns()
        state = self._execution_coord.execute_intent(
            intent=intent,
            order_type=order_type,
            limit_price=limit_price,
            current_time=eval_time,
        )
        t7_ns = time.perf_counter_ns()

        if state is None:
            return

        t8_ns = time.perf_counter_ns()

        # Store in-flight correlation record
        with self._correlation_lock:
            self._in_flight_correlations[state.client_order_id] = {
                "intent": intent,
                "signal": signal,
                "idempotency_key": state.request.idempotency_key,
                "t1_ns": t1_ns,
                "t2_ns": t2_ns,
                "t3_ns": t3_ns,
                "t4_ns": t4_ns,
                "t5_ns": t5_ns,
                "t6_ns": t6_ns,
                "t7_ns": t7_ns,
                "t8_ns": t8_ns,
            }
            pending_fills = self._pending_immediate_fills.pop(state.client_order_id, [])

        # Apply any fills that occurred immediately during adapter submission
        for p_fill in pending_fills:
            state.apply_fill(p_fill)
            if state.is_terminal:
                self._router._registry.release_intent(state.intent_id)

    def _on_paper_fill(self, fill: Fill) -> None:
        """
        Authoritative fill callback wired to PaperExecutionAdapter.
        Updates PositionAccounting, synchronizes PortfolioRuntimeState, and emits telemetry.
        """
        t9_ns = time.perf_counter_ns()

        # 0. Update ExecutionState in router registry (or buffer if in SUBMITTING)
        exec_state = self._router._registry.get_state(fill.client_order_id)
        if exec_state is not None:
            if exec_state.status == CanonicalOrderStatus.SUBMITTING:
                with self._correlation_lock:
                    self._pending_immediate_fills.setdefault(fill.client_order_id, []).append(fill)
            else:
                exec_state.apply_fill(fill)
                if exec_state.is_terminal:
                    self._router._registry.release_intent(exec_state.intent_id)

        # 1. Update position state and cash
        new_pos = self._portfolio_state.on_fill(fill)
        t10_ns = time.perf_counter_ns()

        # 2. Check daily loss
        current_loss = -new_pos.realized_pnl if new_pos.realized_pnl < 0 else 0.0
        self._health.check_daily_loss(current_loss, self._config.max_daily_loss)

        # 3. Retrieve correlation metadata and emit lineage record
        with self._correlation_lock:
            corr = self._in_flight_correlations.pop(fill.client_order_id, None)

        if corr is not None:
            intent = corr["intent"]
            signal = corr["signal"]
            record = RuntimeCorrelationRecord(
                client_order_id=fill.client_order_id,
                intent_id=intent.intent_id,
                signal_id=signal.signal_id,
                signal_fingerprint=signal.fingerprint,
                reaffirmation_key=signal.reaffirmation_key,
                idempotency_key=corr.get("idempotency_key", intent.fingerprint),
                instrument_canonical_id=fill.instrument_id.canonical_id,
                strategy_id=signal.strategy_id,
                t1_market_receive_ns=corr["t1_ns"],
                t2_signal_generated_ns=corr["t2_ns"],
                t3_risk_evaluated_ns=corr["t3_ns"],
                t4_intent_approved_ns=corr["t4_ns"],
                t5_order_planned_ns=corr["t5_ns"],
                t6_order_submitted_ns=corr["t6_ns"],
                t7_wire_dispatched_ns=corr["t7_ns"],
                t8_order_acked_ns=corr["t8_ns"],
                t9_fill_received_ns=t9_ns,
                t10_position_updated_ns=t10_ns,
            )
            self._telemetry.record_lineage(record)
