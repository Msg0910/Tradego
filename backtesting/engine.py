"""
Isolated Backtest Execution Engine for Tradego.

Orchestrates complete deterministic simulation runs using PipelineCoordinator
and PaperExecutionAdapter with strict memory isolation and zero state leakage.
"""

from datetime import datetime
import time
from typing import Any, Dict, List, Optional
import uuid

from services.analytics.engine import FeatureEngine
from services.candles.calendar import IndianMarketCalendar
from services.candles.engine import CandleEngine
from services.execution.accounting import PositionAccounting
from services.execution.config import ExecutionConfig
from services.execution.models import Fill, OrderSide
from services.execution.paper_adapter import PaperExecutionAdapter, TokenBucketRateLimiter
from services.execution.router import ExecutionRouter
from services.execution.state import ExecutionStateRegistry
from services.market_gateway.gateway import MarketDataGateway
from services.market_state.instrument import Exchange, InstrumentId, InstrumentRegistry, InstrumentType
from services.market_state.store import InstrumentStateStore
from services.risk.engine import AdmissionState, RiskEngine
from services.risk.limits import RiskLimits
from services.runtime.config import RuntimeConfig
from services.runtime.coordinator import PipelineCoordinator
from services.runtime.models import RuntimeMode
from services.signals.engine import SignalEngine

from .clock import SimulationClock
from .dataset import HistoricalDataset
from .metrics import PerformanceMetrics
from .models import BacktestConfig, BacktestResult, EquityPoint, TradeRecord
from .replay_controller import ReplayController


class BacktestEngine:
    """
    Isolated execution engine for running backtests over HistoricalDatasets.
    Every call to run() instantiates a fresh, isolated trading runtime.
    """

    def run(self, config: BacktestConfig, dataset: HistoricalDataset) -> BacktestResult:
        if not dataset:
            raise ValueError("Cannot run backtest on an empty HistoricalDataset.")

        start_perf = time.perf_counter()
        run_id = f"{config.run_id_prefix}-{uuid.uuid4().hex[:8].upper()}"

        # 1. Isolated Instrument Registry
        instrument_id = InstrumentId(
            symbol=config.symbol,
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        registry = InstrumentRegistry()
        registry.register(
            instrument_id=instrument_id,
            lot_size=config.lot_size,
            tick_size=config.tick_size,
            provider_tokens={"HISTORICAL_REPLAY": config.symbol, "TEST_PROVIDER": config.symbol},
        )

        # 2. Isolated Pipeline Components
        gateway = MarketDataGateway()
        state_store = InstrumentStateStore(registry=registry)
        calendar = IndianMarketCalendar()
        candle_engine = CandleEngine(registry=registry, calendar=calendar)
        feature_engine = FeatureEngine(registry=registry, candle_engine=candle_engine)
        feature_engine.get_or_create_store(instrument_id)

        signal_engine = SignalEngine(
            registry=registry,
            feature_engine=feature_engine,
            candle_engine=None,
            state_store=state_store,
        )

        # 3. Strategy Instantiation
        strat_cls = config.strategy_class
        try:
            # Check if strategy class accepts config dataclass (e.g. TrendContinuationConfig)
            # Try importing its config class if defined alongside
            module = strat_cls.__module__
            strat_config_cls = getattr(__import__(module, fromlist=["TrendContinuationConfig"]), "TrendContinuationConfig", None)
            if strat_config_cls is not None and config.strategy_params:
                cfg_obj = strat_config_cls(**config.strategy_params)
                strategy = strat_cls(config=cfg_obj)
            elif config.strategy_params:
                strategy = strat_cls(**config.strategy_params)
            else:
                strategy = strat_cls()
        except TypeError:
            strategy = strat_cls()

        signal_engine.register_strategy(instrument_id, strategy)

        # 4. Isolated Risk Engine
        risk_limits = RiskLimits()
        admission_state = AdmissionState()
        risk_engine = RiskEngine(limits=risk_limits, admission_state=admission_state)

        # 5. Isolated Paper Execution Adapter (Unthrottled rate limiter for instant simulation)
        exec_config = ExecutionConfig(
            max_order_notional=50_000_000.0,
            paper_base_slippage_bps=config.slippage_bps,
        )
        exec_registry = ExecutionStateRegistry()
        rate_limiter = TokenBucketRateLimiter(
            rate_limit_per_second=1_000_000.0,
            max_queue_depth=10_000,
            max_wait_ms=0.0,
        )
        paper_adapter = PaperExecutionAdapter(config=exec_config, rate_limiter=rate_limiter)
        router = ExecutionRouter(
            adapter=paper_adapter,
            registry=exec_registry,
            calendar=calendar,
            config=exec_config,
        )

        # 6. Isolated Pipeline Coordinator
        runtime_config = RuntimeConfig(
            runtime_mode=RuntimeMode.PAPER,
            active_symbols=(config.symbol,),
            max_daily_loss=config.max_daily_loss,
        )
        coordinator = PipelineCoordinator(
            config=runtime_config,
            gateway=gateway,
            state_store=state_store,
            candle_engine=candle_engine,
            feature_engine=feature_engine,
            signal_engine=signal_engine,
            risk_engine=risk_engine,
            router=router,
            paper_adapter=paper_adapter,
            registry=registry,
            initial_cash=config.initial_cash,
        )

        # 7. Telemetry & Ledger Tracking
        fills: List[Fill] = []
        trades: List[TradeRecord] = []
        equity_curve: List[EquityPoint] = []

        # Position tracking for trade log construction
        open_pos_qty = 0
        avg_entry_price = 0.0
        opened_ts: Optional[datetime] = None

        def on_fill(fill: Fill) -> None:
            nonlocal open_pos_qty, avg_entry_price, opened_ts
            fills.append(fill)

            fill_dir = 1 if fill.side == OrderSide.BUY else -1
            fill_signed_qty = fill_dir * fill.fill_quantity

            # Check if this fill reduces or closes existing position
            is_reducing = (open_pos_qty > 0 and fill_dir < 0) or (open_pos_qty < 0 and fill_dir > 0)

            if is_reducing:
                reduced_qty = min(abs(open_pos_qty), fill.fill_quantity)
                if open_pos_qty > 0:
                    pnl = (fill.fill_price - avg_entry_price) * reduced_qty
                else:
                    pnl = (avg_entry_price - fill.fill_price) * reduced_qty

                ret_pct = ((fill.fill_price - avg_entry_price) / avg_entry_price * 100.0) if avg_entry_price > 0 else 0.0
                entry_time = opened_ts or fill.exchange_timestamp
                duration = (fill.exchange_timestamp - entry_time).total_seconds()

                trade = TradeRecord(
                    trade_id=f"TRD-{len(trades)+1:05d}",
                    instrument_id=fill.instrument_id,
                    side=OrderSide.BUY if open_pos_qty > 0 else OrderSide.SELL,
                    quantity=reduced_qty,
                    entry_price=round(avg_entry_price, 4),
                    exit_price=round(fill.fill_price, 4),
                    entry_time=entry_time,
                    exit_time=fill.exchange_timestamp,
                    pnl=round(pnl, 2),
                    return_pct=round(ret_pct, 4),
                    holding_duration_seconds=duration,
                    strategy_id=strategy.strategy_id,
                )
                trades.append(trade)

                new_qty = open_pos_qty + fill_signed_qty
                if new_qty == 0:
                    avg_entry_price = 0.0
                    opened_ts = None
                open_pos_qty = new_qty
            else:
                # Position opening or scaling in
                new_qty = open_pos_qty + fill_signed_qty
                total_cost = (abs(open_pos_qty) * avg_entry_price) + (fill.fill_quantity * fill.fill_price)
                avg_entry_price = total_cost / abs(new_qty) if new_qty != 0 else 0.0
                if open_pos_qty == 0:
                    opened_ts = fill.exchange_timestamp
                open_pos_qty = new_qty

            # Capture equity point after fill
            acct = coordinator.portfolio_state.get_account_state()
            eq_pt = EquityPoint(
                timestamp=fill.exchange_timestamp,
                cash=round(acct.available_cash, 2),
                unrealized_pnl=round(acct.unrealized_pnl_current, 2),
                realized_pnl=round(acct.realized_pnl_today, 2),
                total_equity=round(acct.total_equity, 2),
                drawdown_pct=round(acct.drawdown_pct * 100.0, 4),
            )
            equity_curve.append(eq_pt)

        paper_adapter.register_fill_callback(on_fill)

        # 8. Start Coordinator and Initial Equity Point
        coordinator.start()
        start_ts = dataset.start_time
        init_acct = coordinator.portfolio_state.get_account_state()
        equity_curve.append(
            EquityPoint(
                timestamp=start_ts,
                cash=init_acct.available_cash,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                total_equity=init_acct.total_equity,
                drawdown_pct=0.0,
            )
        )

        # 9. Execute Replay
        clock = SimulationClock()
        controller = ReplayController(dataset=dataset, coordinator=coordinator, clock=clock)
        controller.play()

        # Final equity snapshot
        end_ts = dataset.end_time
        final_acct = coordinator.portfolio_state.get_account_state()
        equity_curve.append(
            EquityPoint(
                timestamp=end_ts,
                cash=round(final_acct.available_cash, 2),
                unrealized_pnl=round(final_acct.unrealized_pnl_current, 2),
                realized_pnl=round(final_acct.realized_pnl_today, 2),
                total_equity=round(final_acct.total_equity, 2),
                drawdown_pct=round(final_acct.drawdown_pct * 100.0, 4),
            )
        )

        # 10. Stop Coordinator Gracefully
        coordinator.stop()
        duration_sec = time.perf_counter() - start_perf

        # 11. Compute Metrics
        metrics = PerformanceMetrics.calculate(
            initial_cash=config.initial_cash,
            final_equity=final_acct.total_equity,
            equity_curve=equity_curve,
            trades=trades,
        )

        return BacktestResult(
            run_id=run_id,
            config=config,
            start_time=start_ts,
            end_time=end_ts,
            metrics=metrics,
            equity_curve=equity_curve,
            trades=trades,
            fills=fills,
            execution_duration_seconds=round(duration_sec, 4),
        )
