"""
End-to-End Integration tests for Tradego Paper Trading Runtime (Phase 8).
Verifies complete synchronous pipeline execution across Phases 1 through 7:
MarketEvent -> State -> Candle -> Features -> Strategy -> Signal -> Risk -> Planner -> Router -> PaperAdapter -> Fill -> Accounting -> PortfolioState -> Telemetry.
Also verifies the negative scenario (risk rejection produces zero orders).
"""

from datetime import datetime, timezone
import unittest

from services.analytics.engine import FeatureEngine
from services.candles.calendar import INDIA_TZ, IndianMarketCalendar
from services.candles.engine import CandleEngine
from services.candles.models import Candle
from services.candles.timeframe import TF_1M, TimeFrame
from services.execution.accounting import PositionAccounting
from services.execution.config import ExecutionConfig
from services.execution.models import CanonicalOrderStatus, OrderSide
from services.execution.paper_adapter import PaperExecutionAdapter
from services.execution.router import ExecutionRouter
from services.execution.state import ExecutionStateRegistry
from services.market_gateway.gateway import MarketDataGateway
from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentRegistry, InstrumentType
from services.market_state.store import InstrumentStateStore
from services.risk.engine import AdmissionState, RiskEngine
from services.risk.limits import RiskLimits
from services.signals.base import BaseStrategy
from services.signals.context import StrategyContext
from services.signals.engine import SignalEngine
from services.signals.fingerprint import compute_reaffirmation_key, compute_signal_fingerprint
from services.signals.models import DecisionType, SignalCandidate, SignalType, StrategyDecision, TriggerMode
from services.runtime.config import RuntimeConfig
from services.runtime.coordinator import PipelineCoordinator
from services.runtime.models import RuntimeMode, RuntimeState


class DummyBreakoutStrategy(BaseStrategy):
    """Deterministic test strategy that emits ENTRY_LONG upon observing a closed bar."""

    def __init__(self, strategy_id: str = "DUMMY_BREAKOUT") -> None:
        super().__init__(
            strategy_id=strategy_id,
            strategy_version="1.0.0",
            config_hash="test_config_hash",
            trigger_mode=TriggerMode.BAR_CLOSE,
        )
        self.should_trade = True

    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        if not self.should_trade:
            return StrategyDecision(decision=DecisionType.NO_TRADE)

        if context.active_candle is not None and context.active_candle.timeframe != TF_1M:
            return StrategyDecision(decision=DecisionType.NO_TRADE)

        now = context.evaluation_timestamp
        inst = context.instrument_id
        entry_price = 1000.0
        stop_loss = 980.0
        take_profit = 1040.0

        fp = compute_signal_fingerprint(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            config_hash=self.config_hash,
            instrument_id=inst,
            signal_type=SignalType.ENTRY_LONG.value,
            direction=1,
            trigger_mode=self.trigger_mode.value,
            market_timestamp=now,
            availability_timestamp=now,
            suggested_entry_price=entry_price,
            suggested_stop_loss=stop_loss,
            suggested_take_profit=take_profit,
            regime="BULLISH",
            setup="BREAKOUT",
        )

        rk = compute_reaffirmation_key(
            strategy_id=self.strategy_id,
            instrument_id=inst,
            setup="BREAKOUT",
            direction=1,
            setup_anchor_timestamp=now,
        )

        candidate = SignalCandidate(
            signal_id=f"sig-{now.isoformat()}",
            fingerprint=fp,
            reaffirmation_key=rk,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            config_hash=self.config_hash,
            instrument_id=inst,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            trigger_mode=self.trigger_mode,
            confidence_score=0.90,
            suggested_entry_price=entry_price,
            suggested_stop_loss=stop_loss,
            suggested_take_profit=take_profit,
            risk_reward_ratio=2.0,
            market_timestamp=now,
            availability_timestamp=now,
            generated_timestamp=now,
        )

        return StrategyDecision(decision=DecisionType.TRADE, candidate=candidate)


class TestRuntimeEndToEndPaper(unittest.TestCase):
    """End-to-end integration tests for Tradego Phase 8 paper trading runtime."""

    def setUp(self) -> None:
        self.symbol = "RELIANCE"
        self.instrument_id = InstrumentId(
            symbol=self.symbol,
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.registry = InstrumentRegistry()
        self.registry.register(
            instrument_id=self.instrument_id,
            lot_size=1,
            tick_size=0.05,
            provider_tokens={"TEST_PROVIDER": self.symbol},
        )

        # 1. Gateway
        self.gateway = MarketDataGateway()

        # 2. State Store
        self.state_store = InstrumentStateStore(registry=self.registry)

        # 3. Candle Engine
        self.calendar = IndianMarketCalendar()
        self.candle_engine = CandleEngine(registry=self.registry, calendar=self.calendar)

        # 4. Feature Engine
        self.feature_engine = FeatureEngine(registry=self.registry, candle_engine=self.candle_engine)
        self.feature_engine.get_or_create_store(self.instrument_id)

        # 5. Signal Engine
        self.signal_engine = SignalEngine(
            registry=self.registry,
            feature_engine=self.feature_engine,
            candle_engine=self.candle_engine,
            state_store=self.state_store,
        )
        self.strategy = DummyBreakoutStrategy()
        self.signal_engine.register_strategy(self.instrument_id, self.strategy)

        # 6. Risk Engine
        self.risk_limits = RiskLimits()
        self.admission_state = AdmissionState()
        self.risk_engine = RiskEngine(limits=self.risk_limits, admission_state=self.admission_state)

        # 7. Execution Layer
        self.exec_config = ExecutionConfig(max_order_notional=500000.0)
        self.exec_registry = ExecutionStateRegistry()
        self.paper_adapter = PaperExecutionAdapter(config=self.exec_config)
        self.router = ExecutionRouter(
            adapter=self.paper_adapter,
            registry=self.exec_registry,
            calendar=self.calendar,
            config=self.exec_config,
        )

        # 8. Runtime Coordinator
        self.runtime_config = RuntimeConfig(
            runtime_mode=RuntimeMode.PAPER,
            active_symbols=(self.symbol,),
            max_daily_loss=100000.0,
        )

        self.coordinator = PipelineCoordinator(
            config=self.runtime_config,
            gateway=self.gateway,
            state_store=self.state_store,
            candle_engine=self.candle_engine,
            feature_engine=self.feature_engine,
            signal_engine=self.signal_engine,
            risk_engine=self.risk_engine,
            router=self.router,
            paper_adapter=self.paper_adapter,
            registry=self.registry,
            initial_cash=1_000_000.0,
        )

    def _create_tick(
        self,
        price: float,
        timestamp: datetime,
        bid: float = 999.0,
        ask: float = 1000.0,
    ) -> MarketEvent:
        depth = MarketDepth(
            bids=[DepthLevel(price=bid, quantity=100, orders=1)],
            asks=[DepthLevel(price=ask, quantity=100, orders=1)],
        )
        return MarketEvent(
            provider="TEST_PROVIDER",
            provider_symbol_id=self.symbol,
            symbol=self.symbol,
            exchange_timestamp=timestamp,
            local_receive_datetime=timestamp,
            local_receive_timestamp=1000.0,
            ltp=price,
            bid=bid,
            ask=ask,
            tick_volume=10.0,
            depth=depth,
        )

    def test_end_to_end_paper_trade_lifecycle(self) -> None:
        """
        Tests the positive path:
        MarketEvent -> Candle close -> Strategy -> Signal -> Risk APPROVED ->
        OrderRequest -> PaperAdapter -> Fill -> PositionAccounting -> Telemetry.
        """
        # Start the runtime
        self.coordinator.start()
        self.assertEqual(self.coordinator.lifecycle_state, RuntimeState.RUNNING)

        # 1. Feed ticks within NSE market hours (e.g. 09:15 to 09:20) to close a 5M candle
        # Session: 09:15:00 to 09:20:00
        t0 = datetime(2026, 9, 15, 9, 15, 1, tzinfo=INDIA_TZ)
        tick1 = self._create_tick(price=1005.0, timestamp=t0, bid=1004.0, ask=1005.0)
        self.gateway._on_provider_tick("TEST_PROVIDER", self.symbol, tick1, 1000)

        # Tick at 09:20:00 (closes 09:15-09:20 5M bar and triggers strategy)
        t_close = datetime(2026, 9, 15, 9, 20, 0, tzinfo=INDIA_TZ)
        tick2 = self._create_tick(price=1005.0, timestamp=t_close, bid=1004.0, ask=1005.0)
        self.gateway._on_provider_tick("TEST_PROVIDER", self.symbol, tick2, 2000)

        # Verify Order was created in ExecutionRouter registry
        orders = list(self.exec_registry._states_by_client_id.values())
        self.assertEqual(len(orders), 1, "Expected exactly 1 order submitted from strategy signal")
        order_state = orders[0]
        self.assertEqual(order_state.status, CanonicalOrderStatus.ACKNOWLEDGED)

        # Tick at 09:20:01 matching the resting limit order (Ask=1000.0 matches BUY @ 1000.0)
        t_fill = datetime(2026, 9, 15, 9, 20, 1, tzinfo=INDIA_TZ)
        tick3 = self._create_tick(price=1000.0, timestamp=t_fill, ask=1000.0)
        self.gateway._on_provider_tick("TEST_PROVIDER", self.symbol, tick3, 3000)

        # Verify order transitioned to FILLED
        self.assertEqual(order_state.status, CanonicalOrderStatus.FILLED)

        # Verify PositionAccounting updated PortfolioRuntimeState
        pos = self.coordinator.portfolio_state.get_position(self.instrument_id)
        self.assertIsNotNone(pos)
        self.assertGreater(pos.net_quantity, 0)
        self.assertAlmostEqual(pos.average_entry_price, 1000.0, delta=1.0)

        # Verify TelemetryCollector captured the complete T1 -> T10 lineage record
        records = self.coordinator.telemetry.get_records()
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.client_order_id, order_state.client_order_id)
        self.assertEqual(rec.strategy_id, "DUMMY_BREAKOUT")
        self.assertGreater(rec.t10_position_updated_ns, rec.t1_market_receive_ns)

        # Clean shutdown
        self.coordinator.stop()
        self.assertEqual(self.coordinator.lifecycle_state, RuntimeState.STOPPED)

    def test_negative_scenario_risk_rejection_produces_zero_orders(self) -> None:
        """
        Negative scenario:
        When RiskEngine rejects the signal (e.g. daily loss exceeded),
        NO order is submitted to ExecutionRouter.
        """
        # Configure risk limits with 0 max concurrent positions to force rejection
        self.risk_engine.limits = RiskLimits(max_concurrent_positions=0)

        self.coordinator.start()

        # Feed candle closing ticks
        t0 = datetime(2026, 9, 15, 9, 15, 1, tzinfo=INDIA_TZ)
        t_close = datetime(2026, 9, 15, 9, 20, 0, tzinfo=INDIA_TZ)
        self.gateway._on_provider_tick("TEST_PROVIDER", self.symbol, self._create_tick(1000.0, t0), 1000)
        self.gateway._on_provider_tick("TEST_PROVIDER", self.symbol, self._create_tick(1000.0, t_close), 2000)

        # Verify ZERO orders in execution registry
        orders = list(self.exec_registry._states_by_client_id.values())
        self.assertEqual(len(orders), 0, "Risk rejection must produce ZERO orders in execution registry")

        self.coordinator.stop()


if __name__ == "__main__":
    unittest.main()
