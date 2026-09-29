"""
TradeGo Phase 11 — Advanced Strategies: Backtest Integration & Replay Runner.

Provides canonical integration between Phase 11 Advanced Strategies and the
TradeGo Backtesting & Historical Replay subsystem (PipelineCoordinator,
PaperExecutionAdapter, SimulationClock, ReplayController).
"""

from datetime import datetime
import hashlib
import time
from typing import Any, Dict, List, Optional, Sequence, Type

from backtesting.clock import SimulationClock
from backtesting.dataset import HistoricalDataset
from backtesting.metrics import PerformanceMetrics
from backtesting.models import BacktestConfig, BacktestResult, EquityPoint, TradeRecord
from backtesting.replay_controller import ReplayController
from services.analytics.base import BaseIndicator
from services.analytics.engine import FeatureEngine
from services.analytics.indicators.momentum import StreamingRSI
from services.analytics.indicators.moving_averages import StreamingEMA
from services.analytics.indicators.volatility import RollingBollingerBands, StreamingATR
from services.analytics.indicators.volume import StreamingVWAP, VolumeZScore
from services.candles.calendar import IndianMarketCalendar
from services.candles.engine import CandleEngine
from services.candles.timeframe import TF_5M, TF_15M, TimeFrame
from services.candles.volume import IncrementalVolumePolicy
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
from services.signals.base import BaseStrategy
from services.signals.engine import SignalEngine

from advanced_strategies.mean_reversion import (
    StatisticalMeanReversionConfig,
    StatisticalMeanReversionStrategy,
)
from advanced_strategies.multi_timeframe_trend import (
    MultiTimeframeTrendConfig,
    MultiTimeframeTrendContinuationStrategy,
)
from advanced_strategies.range_breakout import (
    RangeBreakoutConfig,
    RangeBreakoutStrategy,
)


def create_indicator_from_feature_id(feature_id: str) -> Optional[BaseIndicator]:
    """
    Constructs a streaming BaseIndicator instance from a canonical feature ID.
    Supported patterns:
    - EMA_{period}_{TF} (e.g. 'EMA_20_15M', 'EMA_50_5M')
    - RSI_{period}_{TF} (e.g. 'RSI_14_5M')
    - ATR_{period}_{TF} (e.g. 'ATR_14_5M')
    - VWAP_{TF} (e.g. 'VWAP_5M')
    - BB_{period}_{k}_{TF} (e.g. 'BB_20_2_5M', 'BB_20_2.0_5M')
    - VOL_ZSCORE_{period}_{TF} (e.g. 'VOL_ZSCORE_20_5M')
    """
    try:
        parts = feature_id.rsplit("_", 1)
        if len(parts) != 2:
            return None
        name_prefix, tf_str = parts[0], parts[1]
        tf = TimeFrame.from_string(tf_str)

        if name_prefix == "VWAP":
            return StreamingVWAP(timeframe=tf, name="VWAP")

        if name_prefix.startswith("VOL_ZSCORE_"):
            period = int(name_prefix.split("_")[2])
            return VolumeZScore(timeframe=tf, period=period)

        if name_prefix.startswith("BB_"):
            bb_tokens = name_prefix.split("_")
            period = int(bb_tokens[1])
            k = float(bb_tokens[2])
            return RollingBollingerBands(timeframe=tf, period=period, k=k, name=name_prefix)

        if name_prefix.startswith("EMA_"):
            period = int(name_prefix.split("_")[1])
            return StreamingEMA(timeframe=tf, period=period, name=name_prefix)

        if name_prefix.startswith("RSI_"):
            period = int(name_prefix.split("_")[1])
            return StreamingRSI(timeframe=tf, period=period, name=name_prefix)

        if name_prefix.startswith("ATR_"):
            period = int(name_prefix.split("_")[1])
            return StreamingATR(timeframe=tf, period=period, name=name_prefix)
    except Exception:
        return None
    return None


def register_strategy_indicators(
    feature_engine: FeatureEngine,
    instrument_id: InstrumentId,
    strategy: BaseStrategy,
) -> None:
    """
    Registers all streaming indicators required by a Phase 11 advanced strategy
    into the instrument's FeatureStore.
    """
    # 1. Automatic registration via required_features
    if hasattr(strategy, "required_features") and strategy.required_features:
        for fid in strategy.required_features:
            ind = create_indicator_from_feature_id(fid)
            if ind is not None:
                feature_engine.register_indicator(instrument_id, ind)

    # 2. Strategy-specific explicit registration guarantees
    if isinstance(strategy, MultiTimeframeTrendContinuationStrategy):
        cfg: MultiTimeframeTrendConfig = strategy.config
        feature_engine.register_indicator(
            instrument_id,
            StreamingEMA(TF_15M, period=20, name="EMA_20"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingEMA(TF_15M, period=50, name="EMA_50"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingEMA(TF_5M, period=20, name="EMA_20"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingEMA(TF_5M, period=50, name="EMA_50"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingVWAP(TF_5M, name="VWAP"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingRSI(TF_5M, period=14, name="RSI_14"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingATR(TF_5M, period=14, name="ATR_14"),
        )

    elif isinstance(strategy, RangeBreakoutStrategy):
        feature_engine.register_indicator(
            instrument_id,
            RollingBollingerBands(TF_5M, period=20, k=2.0, name="BB_20_2"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingATR(TF_5M, period=14, name="ATR_14"),
        )
        feature_engine.register_indicator(
            instrument_id,
            VolumeZScore(TF_5M, period=20),
        )

    elif isinstance(strategy, StatisticalMeanReversionStrategy):
        feature_engine.register_indicator(
            instrument_id,
            RollingBollingerBands(TF_5M, period=20, k=2.0, name="BB_20_2"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingVWAP(TF_5M, name="VWAP"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingRSI(TF_5M, period=14, name="RSI_14"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingATR(TF_5M, period=14, name="ATR_14"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingEMA(TF_15M, period=20, name="EMA_20"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingEMA(TF_15M, period=50, name="EMA_50"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingEMA(TF_5M, period=20, name="EMA_20"),
        )
        feature_engine.register_indicator(
            instrument_id,
            StreamingEMA(TF_5M, period=50, name="EMA_50"),
        )


def run_advanced_strategy_backtest(
    config: BacktestConfig,
    dataset: HistoricalDataset,
    additional_strategies: Optional[Sequence[BaseStrategy]] = None,
) -> BacktestResult:
    """
    Executes a deterministic backtest of Phase 11 Advanced Strategies against
    a HistoricalDataset using the canonical PipelineCoordinator and PaperExecutionAdapter.

    Preserves:
    - Zero live network or broker calls (PaperExecutionAdapter only).
    - Deterministic simulation clock time strictly from event timestamps.
    - Full telemetry tracking: orders, fills, trades, and portfolio equity curve.
    - Accurate mathematical PerformanceMetrics (Sharpe, Drawdown, Profit Factor, Win Rate).
    """
    if not dataset:
        raise ValueError("Cannot run backtest on an empty HistoricalDataset.")

    start_perf = time.perf_counter()
    start_ts = dataset.start_time
    end_ts = dataset.end_time

    # Deterministic run_id generation (no random/uuid)
    ts_str = start_ts.strftime("%Y%m%d%H%M%S") if start_ts else "00000000000000"
    sym_hash = hashlib.sha256(f"{config.symbol}_{config.strategy_class.__name__}_{ts_str}".encode()).hexdigest()[:8].upper()
    run_id = f"{config.run_id_prefix}-{sym_hash}"

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
    candle_engine = CandleEngine(
        registry=registry,
        calendar=calendar,
        volume_policy_factory=lambda iid: IncrementalVolumePolicy(),
    )
    feature_engine = FeatureEngine(registry=registry, candle_engine=candle_engine)
    feature_engine.get_or_create_store(instrument_id)

    signal_engine = SignalEngine(
        registry=registry,
        feature_engine=feature_engine,
        candle_engine=None,
        state_store=state_store,
    )

    # 3. Strategy Instantiation & Indicator Registration
    strat_cls = config.strategy_class
    try:
        if config.strategy_params:
            primary_strategy = strat_cls(**config.strategy_params)
        else:
            primary_strategy = strat_cls()
    except TypeError:
        primary_strategy = strat_cls()

    all_strategies: List[BaseStrategy] = [primary_strategy]
    if additional_strategies:
        all_strategies.extend(additional_strategies)

    for strat in all_strategies:
        register_strategy_indicators(feature_engine, instrument_id, strat)
        signal_engine.register_strategy(instrument_id, strat)

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

    open_pos_qty = 0
    avg_entry_price = 0.0
    opened_ts: Optional[datetime] = None
    active_strat_id: Optional[str] = None

    def on_fill(fill: Fill) -> None:
        nonlocal open_pos_qty, avg_entry_price, opened_ts, active_strat_id
        fills.append(fill)

        exec_state = router._registry.get_state(fill.client_order_id)
        fill_strat_id = (
            exec_state.request.strategy_id
            if (exec_state and exec_state.request and exec_state.request.strategy_id)
            else primary_strategy.strategy_id
        )

        fill_dir = 1 if fill.side == OrderSide.BUY else -1
        fill_signed_qty = fill_dir * fill.fill_quantity

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
                holding_duration_seconds=round(duration, 4),
                strategy_id=active_strat_id or fill_strat_id,
            )
            trades.append(trade)

            new_qty = open_pos_qty + fill_signed_qty
            if new_qty == 0:
                avg_entry_price = 0.0
                opened_ts = None
                active_strat_id = None
            open_pos_qty = new_qty
        else:
            new_qty = open_pos_qty + fill_signed_qty
            total_cost = (abs(open_pos_qty) * avg_entry_price) + (fill.fill_quantity * fill.fill_price)
            avg_entry_price = total_cost / abs(new_qty) if new_qty != 0 else 0.0
            if open_pos_qty == 0:
                opened_ts = fill.exchange_timestamp
                active_strat_id = fill_strat_id
            open_pos_qty = new_qty

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
    init_acct = coordinator.portfolio_state.get_account_state()
    equity_curve.append(
        EquityPoint(
            timestamp=start_ts or datetime.min,
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
    final_acct = coordinator.portfolio_state.get_account_state()
    equity_curve.append(
        EquityPoint(
            timestamp=end_ts or datetime.min,
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
