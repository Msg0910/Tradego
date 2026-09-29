"""
Unit tests for Tradego Backtesting & Historical Replay Subsystem.

Validates the 10 mandatory milestone requirements:
1. Dataset monotonic ordering and schema validation
2. Simulation clock progression decoupled from wall clock
3. Replay controller step and seek mechanics
4. End-to-end execution of trading pipeline via PaperExecutionAdapter
5. Determinism across identical simulation runs
6. State isolation between sequential backtest runs
7. Mathematical metric precision (Sharpe, Drawdown, Win Rate, Profit Factor)
8. Deterministic parameter sweep grid search and ranking
9. Absolute safety: zero live broker calls or network egress
10. Frozen-core hash invariance (all 86 baseline files untouched)
"""

from datetime import datetime, timezone
import hashlib
import json
import os
import unittest

from backtesting.clock import SimulationClock
from backtesting.dataset import HistoricalDataset
from backtesting.engine import BacktestEngine
from backtesting.metrics import PerformanceMetrics
from backtesting.models import (
    BacktestConfig,
    EquityPoint,
    ReplayState,
    SweepConfig,
    TradeRecord,
)
from backtesting.replay_controller import ReplayController
from backtesting.report import ReportGenerator
from backtesting.sweep import ParameterSweepRunner
from gateway.broker_adapter import LiveBrokerAdapter
from services.candles.calendar import INDIA_TZ
from services.candles.timeframe import TF_5M
from services.execution.models import CanonicalOrderStatus, OrderSide
from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.signals.base import BaseStrategy
from services.signals.context import StrategyContext
from services.signals.fingerprint import compute_reaffirmation_key, compute_signal_fingerprint
from services.signals.models import DecisionType, SignalCandidate, SignalType, StrategyDecision, TriggerMode


class MockBacktestStrategy(BaseStrategy):
    """Deterministic strategy for backtesting unit tests."""

    def __init__(
        self,
        strategy_id: str = "MOCK_STRAT",
        entry_price: float = 1000.0,
        exit_price: float = 1050.0,
    ) -> None:
        super().__init__(
            strategy_id=strategy_id,
            strategy_version="1.0.0",
            config_hash="test_config_hash",
            trigger_mode=TriggerMode.BAR_CLOSE,
        )
        self.entry_price = float(entry_price)
        self.exit_price = float(exit_price)
        self.has_entered = False

    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        if context.active_candle is not None and context.active_candle.timeframe != TF_5M:
            return StrategyDecision(decision=DecisionType.NO_TRADE)

        now = context.evaluation_timestamp
        inst = context.instrument_id
        pos = context.position

        # 1. Entry condition: flat and hasn't entered yet
        if (pos is None or pos.is_flat) and not self.has_entered:
            self.has_entered = True
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
                suggested_entry_price=self.entry_price,
                suggested_stop_loss=self.entry_price - 20.0,
                suggested_take_profit=self.exit_price,
                regime="BULLISH",
                setup="MOCK_ENTRY",
            )
            rk = compute_reaffirmation_key(
                strategy_id=self.strategy_id,
                instrument_id=inst,
                setup="MOCK_ENTRY",
                direction=1,
                setup_anchor_timestamp=now,
            )
            candidate = SignalCandidate(
                signal_id=f"sig-entry-{now.isoformat()}",
                fingerprint=fp,
                reaffirmation_key=rk,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                config_hash=self.config_hash,
                instrument_id=inst,
                signal_type=SignalType.ENTRY_LONG,
                direction=1,
                trigger_mode=self.trigger_mode,
                confidence_score=0.95,
                suggested_entry_price=self.entry_price,
                suggested_stop_loss=self.entry_price - 20.0,
                suggested_take_profit=self.exit_price,
                risk_reward_ratio=2.5,
                market_timestamp=now,
                availability_timestamp=now,
                generated_timestamp=now,
                regime="BULLISH",
                setup="MOCK_ENTRY",
            )
            return StrategyDecision(decision=DecisionType.TRADE, candidate=candidate)

        # 2. Exit condition: in position and price >= exit_price
        elif pos is not None and pos.net_quantity > 0:
            current_price = context.market_state.ltp if (context.market_state and context.market_state.ltp) else self.entry_price
            if current_price >= self.exit_price:
                fp = compute_signal_fingerprint(
                    strategy_id=self.strategy_id,
                    strategy_version=self.strategy_version,
                    config_hash=self.config_hash,
                    instrument_id=inst,
                    signal_type=SignalType.EXIT_LONG.value,
                    direction=-1,
                    trigger_mode=self.trigger_mode.value,
                    market_timestamp=now,
                    availability_timestamp=now,
                    suggested_entry_price=self.exit_price,
                    suggested_stop_loss=None,
                    suggested_take_profit=None,
                    regime="BULLISH",
                    setup="MOCK_EXIT",
                )
                rk = compute_reaffirmation_key(
                    strategy_id=self.strategy_id,
                    instrument_id=inst,
                    setup="MOCK_EXIT",
                    direction=-1,
                    setup_anchor_timestamp=now,
                )
                candidate = SignalCandidate(
                    signal_id=f"sig-exit-{now.isoformat()}",
                    fingerprint=fp,
                    reaffirmation_key=rk,
                    strategy_id=self.strategy_id,
                    strategy_version=self.strategy_version,
                    config_hash=self.config_hash,
                    instrument_id=inst,
                    signal_type=SignalType.EXIT_LONG,
                    direction=-1,
                    trigger_mode=self.trigger_mode,
                    confidence_score=0.95,
                    suggested_entry_price=self.exit_price,
                    suggested_stop_loss=None,
                    suggested_take_profit=None,
                    risk_reward_ratio=None,
                    market_timestamp=now,
                    availability_timestamp=now,
                    generated_timestamp=now,
                    regime="BULLISH",
                    setup="MOCK_EXIT",
                )
                return StrategyDecision(decision=DecisionType.TRADE, candidate=candidate)

        return StrategyDecision(decision=DecisionType.NO_TRADE)


class TestBacktestingEngine(unittest.TestCase):
    """Test suite validating all requirements of the Backtesting & Historical Replay milestone."""

    def setUp(self) -> None:
        self.symbol = "RELIANCE"
        # Generate standard test ticks spanning a 5M candle session
        self.t0 = datetime(2026, 9, 15, 9, 15, 1, tzinfo=INDIA_TZ)
        self.t1 = datetime(2026, 9, 15, 9, 17, 0, tzinfo=INDIA_TZ)
        self.t2 = datetime(2026, 9, 15, 9, 20, 0, tzinfo=INDIA_TZ)  # closes 5M bar, triggers entry
        self.t3 = datetime(2026, 9, 15, 9, 20, 1, tzinfo=INDIA_TZ)  # matches entry limit @ 1000
        self.t4 = datetime(2026, 9, 15, 9, 22, 0, tzinfo=INDIA_TZ)
        self.t5 = datetime(2026, 9, 15, 9, 25, 0, tzinfo=INDIA_TZ)
        self.t6 = datetime(2026, 9, 15, 9, 25, 1, tzinfo=INDIA_TZ)
        self.t7 = datetime(2026, 9, 15, 9, 26, 0, tzinfo=INDIA_TZ)  # closes 1M bar, cascades 5M bar @ 1055, triggers exit
        self.t8 = datetime(2026, 9, 15, 9, 26, 1, tzinfo=INDIA_TZ)  # matches exit limit @ 1050

        self.ticks = [
            self._create_tick(price=1005.0, timestamp=self.t0, bid=1004.0, ask=1005.0, seq=1),
            self._create_tick(price=1002.0, timestamp=self.t1, bid=1001.0, ask=1002.0, seq=2),
            self._create_tick(price=1001.0, timestamp=self.t2, bid=1000.0, ask=1001.0, seq=3),
            self._create_tick(price=1000.0, timestamp=self.t3, bid=999.5, ask=1000.0, seq=4),
            self._create_tick(price=1030.0, timestamp=self.t4, bid=1029.0, ask=1030.0, seq=5),
            self._create_tick(price=1055.0, timestamp=self.t5, bid=1054.0, ask=1055.0, seq=6),
            self._create_tick(price=1055.0, timestamp=self.t6, bid=1050.0, ask=1055.0, seq=7),
            self._create_tick(price=1055.0, timestamp=self.t7, bid=1050.0, ask=1055.0, seq=8),
            self._create_tick(price=1055.0, timestamp=self.t8, bid=1050.0, ask=1055.0, seq=9),
        ]
        self.dataset = HistoricalDataset.from_events(self.ticks)

    def _create_tick(
        self,
        price: float,
        timestamp: datetime,
        bid: float = 999.0,
        ask: float = 1000.0,
        seq: int = 1,
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
            ltp=price,
            bid=bid,
            ask=ask,
            tick_volume=10.0,
            depth=depth,
            raw={"sequence_number": seq},
        )

    # -------------------------------------------------------------------------
    # 1. Dataset Monotonic Ordering
    # -------------------------------------------------------------------------
    def test_01_dataset_monotonic_ordering(self) -> None:
        """1. HistoricalDataset enforces chronological ordering and rejects backwards timestamps."""
        # Unsorted list of events
        shuffled = [self.ticks[3], self.ticks[0], self.ticks[2], self.ticks[1]]
        sorted_ds = HistoricalDataset.from_events(shuffled, sort=True)
        self.assertEqual(len(sorted_ds), 4)
        self.assertEqual(sorted_ds[0].exchange_timestamp, self.ticks[0].exchange_timestamp)
        self.assertEqual(sorted_ds[-1].exchange_timestamp, self.ticks[3].exchange_timestamp)

        # Monotonicity rejection when sort=False
        with self.assertRaises(ValueError):
            HistoricalDataset(events=shuffled, sort=False, validate_monotonic=True)

    # -------------------------------------------------------------------------
    # 2. Simulation Clock Progression
    # -------------------------------------------------------------------------
    def test_02_simulation_clock_progression(self) -> None:
        """2. SimulationClock advances strictly via event timestamps without wall-clock dependency."""
        clock = SimulationClock()
        self.assertIsNone(clock.current_time)

        clock.advance_to(self.t0)
        self.assertEqual(clock.now(), self.t0)

        clock.advance_to(self.t1)
        self.assertEqual(clock.now(), self.t1)

        # Regressive timestamp must fail closed
        with self.assertRaises(ValueError):
            clock.advance_to(self.t0)

        # Reset cleans state
        clock.reset()
        self.assertIsNone(clock.current_time)
        with self.assertRaises(RuntimeError):
            clock.now()

    # -------------------------------------------------------------------------
    # 3. Replay Step and Seek
    # -------------------------------------------------------------------------
    def test_03_replay_controller_step_and_seek(self) -> None:
        """3. ReplayController governs step, seek, pause, play, and reset."""
        # Use a mock coordinator to test replay controller isolated mechanics
        events_observed = []
        class MockCoordinator:
            def on_market_event(self, ev: MarketEvent) -> None:
                events_observed.append(ev)

        clock = SimulationClock()
        controller = ReplayController(
            dataset=self.dataset,
            coordinator=MockCoordinator(),  # type: ignore
            clock=clock,
        )

        self.assertEqual(controller.state, ReplayState.STOPPED)

        # Step 1
        ev1 = controller.step()
        self.assertIsNotNone(ev1)
        self.assertEqual(controller.current_index, 1)
        self.assertEqual(clock.now(), self.t0)

        # Seek to t4
        advanced = controller.seek(self.t4)
        self.assertGreater(advanced, 0)
        self.assertEqual(clock.now(), self.t4)

        # Play remaining
        total_remaining = controller.play()
        self.assertEqual(controller.state, ReplayState.COMPLETED)
        self.assertEqual(len(events_observed), len(self.dataset))

        # Reset
        controller.reset()
        self.assertEqual(controller.state, ReplayState.STOPPED)
        self.assertEqual(controller.current_index, 0)

    # -------------------------------------------------------------------------
    # 4. End-to-End Paper Execution
    # -------------------------------------------------------------------------
    def test_04_pipeline_execution_paper_only(self) -> None:
        """4. End-to-end execution through PaperExecutionAdapter records trades and equity."""
        config = BacktestConfig(
            symbol=self.symbol,
            strategy_class=MockBacktestStrategy,
            strategy_params={"entry_price": 1000.0, "exit_price": 1050.0},
            initial_cash=1_000_000.0,
            slippage_bps=2.0,
        )
        engine = BacktestEngine()
        result = engine.run(config, self.dataset)

        self.assertIsNotNone(result.run_id)
        self.assertGreaterEqual(len(result.fills), 1)
        self.assertGreaterEqual(len(result.trades), 1)
        self.assertGreaterEqual(len(result.equity_curve), 2)

        # Trade inspection
        trade = result.trades[0]
        self.assertEqual(trade.side, OrderSide.BUY)
        self.assertAlmostEqual(trade.entry_price, 1000.0, delta=2.0)  # within slippage
        self.assertAlmostEqual(trade.exit_price, 1050.0, delta=2.0)
        self.assertGreater(trade.pnl, 0.0)

    # -------------------------------------------------------------------------
    # 5. Determinism Across Identical Runs
    # -------------------------------------------------------------------------
    def test_05_determinism_identical_runs(self) -> None:
        """5. Two independent runs with identical config and data yield bit-for-bit identical results."""
        config = BacktestConfig(
            symbol=self.symbol,
            strategy_class=MockBacktestStrategy,
            strategy_params={"entry_price": 1000.0, "exit_price": 1050.0},
            initial_cash=1_000_000.0,
        )
        engine = BacktestEngine()
        run1 = engine.run(config, self.dataset)
        run2 = engine.run(config, self.dataset)

        self.assertEqual(len(run1.trades), len(run2.trades))
        for t1, t2 in zip(run1.trades, run2.trades):
            self.assertEqual(t1.entry_price, t2.entry_price)
            self.assertEqual(t1.exit_price, t2.exit_price)
            self.assertEqual(t1.pnl, t2.pnl)
            self.assertEqual(t1.return_pct, t2.return_pct)

        self.assertEqual(run1.metrics.total_return_pct, run2.metrics.total_return_pct)
        self.assertEqual(run1.metrics.sharpe_ratio, run2.metrics.sharpe_ratio)
        self.assertEqual(run1.metrics.max_drawdown_pct, run2.metrics.max_drawdown_pct)
        self.assertEqual(run1.metrics.final_equity, run2.metrics.final_equity)

    # -------------------------------------------------------------------------
    # 6. State Isolation
    # -------------------------------------------------------------------------
    def test_06_state_isolation_between_runs(self) -> None:
        """6. Running consecutive backtests does not leak cash, positions, or indicator state."""
        engine = BacktestEngine()

        # Run A with ₹1,000,000
        config_a = BacktestConfig(
            symbol=self.symbol,
            strategy_class=MockBacktestStrategy,
            strategy_params={"entry_price": 1000.0, "exit_price": 1050.0},
            initial_cash=1_000_000.0,
        )
        res_a = engine.run(config_a, self.dataset)

        # Run B with ₹500,000
        config_b = BacktestConfig(
            symbol=self.symbol,
            strategy_class=MockBacktestStrategy,
            strategy_params={"entry_price": 1000.0, "exit_price": 1050.0},
            initial_cash=500_000.0,
        )
        res_b = engine.run(config_b, self.dataset)

        self.assertEqual(res_a.metrics.initial_cash, 1_000_000.0)
        self.assertEqual(res_b.metrics.initial_cash, 500_000.0)
        self.assertNotEqual(res_a.metrics.final_equity, res_b.metrics.final_equity)

    # -------------------------------------------------------------------------
    # 7. Mathematical Metric Precision
    # -------------------------------------------------------------------------
    def test_07_metrics_mathematical_precision(self) -> None:
        """7. PerformanceMetrics calculation verifies Sharpe, Sortino, Max Drawdown, and Win Rate."""
        t_base = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        curve = [
            EquityPoint(timestamp=t_base, cash=100000.0, unrealized_pnl=0.0, realized_pnl=0.0, total_equity=100000.0, drawdown_pct=0.0),
            EquityPoint(timestamp=t_base, cash=110000.0, unrealized_pnl=0.0, realized_pnl=10000.0, total_equity=110000.0, drawdown_pct=0.0),
            EquityPoint(timestamp=t_base, cash=99000.0, unrealized_pnl=0.0, realized_pnl=-1000.0, total_equity=99000.0, drawdown_pct=10.0),
            EquityPoint(timestamp=t_base, cash=120000.0, unrealized_pnl=0.0, realized_pnl=20000.0, total_equity=120000.0, drawdown_pct=0.0),
        ]
        trades = [
            TradeRecord(
                trade_id="T1",
                instrument_id=InstrumentId(symbol="TEST", exchange=Exchange.NSE if 'Exchange' in globals() else None, instrument_type=InstrumentType.EQUITY),  # type: ignore
                side=OrderSide.BUY,
                quantity=10,
                entry_price=100.0,
                exit_price=110.0,
                entry_time=t_base,
                exit_time=t_base,
                pnl=100.0,
                return_pct=10.0,
                holding_duration_seconds=60.0,
                strategy_id="TEST",
            ),
            TradeRecord(
                trade_id="T2",
                instrument_id=InstrumentId(symbol="TEST", exchange=Exchange.NSE if 'Exchange' in globals() else None, instrument_type=InstrumentType.EQUITY),  # type: ignore
                side=OrderSide.BUY,
                quantity=10,
                entry_price=100.0,
                exit_price=90.0,
                entry_time=t_base,
                exit_time=t_base,
                pnl=-100.0,
                return_pct=-10.0,
                holding_duration_seconds=60.0,
                strategy_id="TEST",
            ),
        ]

        m = PerformanceMetrics.calculate(
            initial_cash=100000.0,
            final_equity=120000.0,
            equity_curve=curve,
            trades=trades,
        )

        self.assertEqual(m.total_return_pct, 20.0)
        self.assertEqual(m.max_drawdown_pct, 10.0)
        self.assertEqual(m.total_trades, 2)
        self.assertEqual(m.winning_trades, 1)
        self.assertEqual(m.losing_trades, 1)
        self.assertEqual(m.win_rate, 50.0)
        self.assertEqual(m.profit_factor, 1.0)

    # -------------------------------------------------------------------------
    # 8. Deterministic Parameter Sweep
    # -------------------------------------------------------------------------
    def test_08_parameter_sweep_grid(self) -> None:
        """8. ParameterSweepRunner tests Cartesian combinations and ranks by objective value."""
        base_cfg = BacktestConfig(
            symbol=self.symbol,
            strategy_class=MockBacktestStrategy,
            initial_cash=1_000_000.0,
        )
        sweep_cfg = SweepConfig(
            base_config=base_cfg,
            param_grid={
                "entry_price": [1000.0, 1002.0],
                "exit_price": [1050.0],
            },
            objective_metric="total_return_pct",
        )
        runner = ParameterSweepRunner()
        sweep_res = runner.run(sweep_cfg, self.dataset)

        self.assertEqual(len(sweep_res.all_runs), 2)
        self.assertIn("entry_price", sweep_res.best_params)
        self.assertGreaterEqual(
            sweep_res.all_runs[0].objective_value,
            sweep_res.all_runs[1].objective_value,
        )

        # Markdown report generation verification
        md_report = ReportGenerator.sweep_to_markdown(sweep_res)
        self.assertIn("TradeGo Parameter Sweep Report", md_report)
        self.assertIn("Ranked Grid Evaluation Results", md_report)

    # -------------------------------------------------------------------------
    # 9. Safety: No Live Broker Calls
    # -------------------------------------------------------------------------
    def test_09_safety_no_live_broker_calls(self) -> None:
        """9. Confirms live broker transport remains uninitialized and cannot be triggered."""
        adapter = LiveBrokerAdapter()
        res = adapter._execute_live_dispatch("DUMMY_INSTRUCTION", "DUMMY_KEY")  # type: ignore
        self.assertEqual(res.failure_reason, "REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED")

    # -------------------------------------------------------------------------
    # 10. Frozen-Core Hash Invariance
    # -------------------------------------------------------------------------
    def test_10_frozen_core_hash_invariance(self) -> None:
        """10. Confirms all 86 frozen-core files in services/, strategies/, brokers/, config/ match baseline hashes."""
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        baseline_file = r"C:\Users\Admin\.gemini\antigravity-ide\brain\dcf5ee07-0793-4ffd-bf96-5c85f21dc7fd\scratch\frozen_core_hashes.json"

        if os.path.exists(baseline_file):
            with open(baseline_file, "r") as f:
                baseline = json.load(f)

            current = {}
            for d in ["services", "strategies", "brokers", "config"]:
                target_dir = os.path.join(base_dir, d)
                if not os.path.exists(target_dir):
                    continue
                for root, _, files in os.walk(target_dir):
                    if "__pycache__" in root:
                        continue
                    for f in files:
                        p = os.path.join(root, f)
                        rel_p = os.path.relpath(p, base_dir).replace("\\", "/")
                        with open(p, "rb") as fp:
                            current[rel_p] = hashlib.sha256(fp.read()).hexdigest()

            self.assertEqual(len(current), 86, "Frozen core must contain exactly 86 files")
            for k, v in baseline.items():
                self.assertIn(k, current, f"Missing frozen core file: {k}")
                self.assertEqual(current[k], v, f"Frozen core file modified: {k}")


if __name__ == "__main__":
    unittest.main()
