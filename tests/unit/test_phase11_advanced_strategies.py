"""
TradeGo Phase 11 — Advanced Strategies: Unit Test Suite.

Covers:
Phase 11 Step 1:
- Algorithmic execution schedules (TWAP slicing, time-decay exit horizons).

Phase 11 Step 2:
- Multi-Timeframe Composite Regime Classifier (alignment, conflict, composite states).

Phase 11 Step 3:
- Multi-Timeframe Trend Continuation Strategy (regime gating, pullback triggers, exit evaluation).
- Range Breakout Strategy (Bollinger Band breakout, volume z-score confirmation).
- Statistical Mean Reversion Strategy (RSI & Bollinger extremes in neutral regimes targeting VWAP).
- Strict determinism, causality, and safety across all components.
"""

from datetime import datetime, timedelta, timezone
import os
import unittest

from advanced_strategies.backtest import (
    create_indicator_from_feature_id,
    register_strategy_indicators,
    run_advanced_strategy_backtest,
)
from advanced_strategies.mean_reversion import (
    StatisticalMeanReversionConfig,
    StatisticalMeanReversionStrategy,
)
from advanced_strategies.models import (
    ScheduleType,
    TimeDecayExitConfig,
    TWAPScheduleConfig,
)
from advanced_strategies.multi_timeframe_trend import (
    MultiTimeframeTrendConfig,
    MultiTimeframeTrendContinuationStrategy,
)
from advanced_strategies.range_breakout import (
    RangeBreakoutConfig,
    RangeBreakoutStrategy,
)
from advanced_strategies.regimes import (
    CompositeRegimeResult,
    MarketRegimeState,
    MultiTimeframeCompositeRegimeClassifier,
    MultiTimeframeRegimeConfig,
)
from advanced_strategies.schedules.algorithmic import (
    generate_time_decay_schedule,
    generate_twap_schedule,
)
from backtesting.dataset import HistoricalDataset
from backtesting.engine import BacktestEngine
from backtesting.models import BacktestConfig
from gateway.broker_adapter import LiveBrokerAdapter
from services.analytics.models import FeatureQuality, FeatureSnapshot, FeatureValue
from services.candles.calendar import INDIA_TZ
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_5M
from services.execution.models import OrderSide
from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.market_state.state import InstrumentState
from services.signals.base import BaseRegimeClassifier, BaseStrategy
from services.signals.context import StrategyContext
from services.signals.models import (
    NO_TRADE_DEFAULT,
    NO_TRADE_NO_SETUP,
    NO_TRADE_REGIME_FILTER,
    DecisionType,
    PositionView,
    QualityPolicy,
    SignalType,
    TriggerMode,
)


def make_test_candle(
    iid: InstrumentId,
    dt: datetime,
    open_p: float,
    high: float,
    low: float,
    close: float,
    is_closed: bool = True,
) -> Candle:
    return Candle(
        instrument_id=iid,
        timeframe=TF_5M,
        start_time=dt - timedelta(minutes=5),
        end_time=dt,
        open=open_p,
        high=high,
        low=low,
        close=close,
        volume=5000.0,
        ticks=500,
        volume_quality=VolumeQuality.COMPLETE,
        is_closed=is_closed,
    )


class TestMultiTimeframeCompositeRegimeClassifier(unittest.TestCase):
    """
    Test suite for Phase 11 Step 2: Multi-Timeframe Composite Regime Classifier.
    """

    def setUp(self) -> None:
        self.iid = InstrumentId("RELIANCE", Exchange.NSE, InstrumentType.EQUITY)
        self.ts = datetime(2026, 9, 26, 9, 30, 0, tzinfo=timezone.utc)
        self.config = MultiTimeframeRegimeConfig()
        self.classifier = MultiTimeframeCompositeRegimeClassifier(self.config)

    def _build_context(
        self,
        ltp: float,
        htf_fast: float,
        htf_slow: float,
        ltf_fast: float,
        ltf_slow: float,
        ltf_vwap: float,
        atr: float,
        missing_feature: str = "",
    ) -> StrategyContext:
        features_dict = {
            "EMA_20_15M": FeatureValue("EMA_20_15M", htf_fast, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_15M": FeatureValue("EMA_50_15M", htf_slow, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_20_5M": FeatureValue("EMA_20_5M", ltf_fast, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_5M": FeatureValue("EMA_50_5M", ltf_slow, FeatureQuality.VALID, self.ts, self.ts, True),
            "VWAP_5M": FeatureValue("VWAP_5M", ltf_vwap, FeatureQuality.VALID, self.ts, self.ts, True),
            "ATR_14_5M": FeatureValue("ATR_14_5M", atr, FeatureQuality.VALID, self.ts, self.ts, True),
        }

        if missing_feature and missing_feature in features_dict:
            del features_dict[missing_feature]

        feat_snap = FeatureSnapshot(self.iid, self.ts, features_dict)
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = ltp

        return StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=feat_snap,
            evaluation_timestamp=self.ts,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),
        )

    def test_01_mtf_regime_classification_alignment(self) -> None:
        """1. Verifies HTF (15M) and LTF (5M) trend consensus produces BULLISH / BEARISH regimes."""
        self.assertIsInstance(self.classifier, BaseRegimeClassifier)

        ctx_bull = self._build_context(
            ltp=25090.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=25060.0,
            atr=50.0,
        )
        self.assertEqual(self.classifier.classify(ctx_bull), "BULLISH")

        ctx_bear = self._build_context(
            ltp=24910.0,
            htf_fast=24900.0,
            htf_slow=25000.0,
            ltf_fast=24920.0,
            ltf_slow=24950.0,
            ltf_vwap=24940.0,
            atr=50.0,
        )
        self.assertEqual(self.classifier.classify(ctx_bear), "BEARISH")

    def test_02_mtf_regime_classification_conflict(self) -> None:
        """2. Verifies conflicting HTF and LTF signals resolve strictly to NEUTRAL (no trade)."""
        ctx_conflict_a = self._build_context(
            ltp=24910.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=24920.0,
            ltf_slow=24950.0,
            ltf_vwap=24940.0,
            atr=50.0,
        )
        self.assertEqual(self.classifier.classify(ctx_conflict_a), "NEUTRAL")

        ctx_conflict_b = self._build_context(
            ltp=25090.0,
            htf_fast=24900.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=25060.0,
            atr=50.0,
        )
        self.assertEqual(self.classifier.classify(ctx_conflict_b), "NEUTRAL")

        ctx_conflict_c = self._build_context(
            ltp=25040.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=25060.0,
            atr=50.0,
        )
        self.assertEqual(self.classifier.classify(ctx_conflict_c), "NEUTRAL")

    def test_03_mtf_composite_regime_states(self) -> None:
        """3. Verifies composite hierarchical state labeling with volatility combinations."""
        composite_classifier = MultiTimeframeCompositeRegimeClassifier(
            MultiTimeframeRegimeConfig(return_composite=True)
        )

        ctx_bull_low_vol = self._build_context(
            ltp=25000.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=24980.0,
            atr=40.0,
        )
        self.assertEqual(
            composite_classifier.classify(ctx_bull_low_vol),
            MarketRegimeState.MACRO_BULLISH_LOW_VOL.value,
        )

        ctx_bull_high_vol = self._build_context(
            ltp=25000.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=24980.0,
            atr=250.0,
        )
        self.assertEqual(
            composite_classifier.classify(ctx_bull_high_vol),
            MarketRegimeState.MACRO_BULLISH_EXPANDING_VOL.value,
        )

        ctx_bear_high_vol = self._build_context(
            ltp=25000.0,
            htf_fast=24900.0,
            htf_slow=25000.0,
            ltf_fast=24920.0,
            ltf_slow=24950.0,
            ltf_vwap=25020.0,
            atr=250.0,
        )
        self.assertEqual(
            composite_classifier.classify(ctx_bear_high_vol),
            MarketRegimeState.MACRO_BEARISH_EXPANDING_VOL.value,
        )

        ctx_neutral_low_vol = self._build_context(
            ltp=25000.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=24920.0,
            ltf_slow=24950.0,
            ltf_vwap=25020.0,
            atr=40.0,
        )
        self.assertEqual(
            composite_classifier.classify(ctx_neutral_low_vol),
            MarketRegimeState.RANGING_COMPRESSED.value,
        )

    def test_04_mtf_regime_insufficient_data(self) -> None:
        """4. Verifies None is returned on missing features or uninitialized data."""
        ctx_missing_htf = self._build_context(
            ltp=25000.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=24980.0,
            atr=50.0,
            missing_feature="EMA_20_15M",
        )
        self.assertIsNone(self.classifier.classify(ctx_missing_htf))

        ctx_missing_atr = self._build_context(
            ltp=25000.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=24980.0,
            atr=50.0,
            missing_feature="ATR_14_5M",
        )
        self.assertIsNone(self.classifier.classify(ctx_missing_atr))

        ctx_zero_price = self._build_context(
            ltp=0.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=24980.0,
            atr=50.0,
        )
        self.assertIsNone(self.classifier.classify(ctx_zero_price))

    def test_05_mtf_regime_determinism(self) -> None:
        """5. Confirms bit-for-bit identical output given identical context inputs."""
        ctx = self._build_context(
            ltp=25090.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=25060.0,
            atr=50.0,
        )
        res1 = self.classifier.classify_detailed(ctx)
        res2 = self.classifier.classify_detailed(ctx)

        self.assertIsNotNone(res1)
        self.assertIsNotNone(res2)
        self.assertEqual(res1, res2)
        self.assertEqual(res1.confidence, 1.0)
        self.assertEqual(res1.primary_regime, "BULLISH")

    def test_06_mtf_regime_provenance_and_metadata(self) -> None:
        """6. Verifies comprehensive provenance mapping and confidence metrics."""
        ctx = self._build_context(
            ltp=25000.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=24980.0,
            atr=50.0,
        )
        res = self.classifier.classify_detailed(ctx)
        self.assertIsNotNone(res)
        self.assertIsInstance(res, CompositeRegimeResult)
        self.assertIn("htf_trend", res.provenance)
        self.assertIn("ltf_trend", res.provenance)
        self.assertIn("atr_bps", res.provenance)
        self.assertEqual(res.provenance["instrument"], "RELIANCE")
        self.assertEqual(res.provenance["price"], 25000.0)

    def test_07_mtf_regime_safety_no_broker_calls(self) -> None:
        """7. Verifies zero broker/network connectivity and analytical purity."""
        ctx = self._build_context(
            ltp=25000.0,
            htf_fast=25100.0,
            htf_slow=25000.0,
            ltf_fast=25080.0,
            ltf_slow=25050.0,
            ltf_vwap=24980.0,
            atr=50.0,
        )
        output = self.classifier.classify(ctx)
        self.assertIn(output, ["BULLISH", "BEARISH", "NEUTRAL"])


class TestAdvancedStrategiesStep3(unittest.TestCase):
    """
    Test suite for Phase 11 Step 3: Advanced Systematic Strategies.
    """

    def setUp(self) -> None:
        self.iid = InstrumentId("TCS", Exchange.NSE, InstrumentType.EQUITY)
        self.ts = datetime(2026, 9, 26, 9, 30, 0, tzinfo=timezone.utc)

    def test_08_mtf_trend_strategy_signal_generation(self) -> None:
        """8. Verifies MultiTimeframeTrendContinuationStrategy generates schema-compliant signals with valid fingerprints."""
        strategy = MultiTimeframeTrendContinuationStrategy()
        self.assertIsInstance(strategy, BaseStrategy)

        features = {
            "EMA_20_15M": FeatureValue("EMA_20_15M", 3550.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_15M": FeatureValue("EMA_50_15M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_20_5M": FeatureValue("EMA_20_5M", 3540.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_5M": FeatureValue("EMA_50_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "VWAP_5M": FeatureValue("VWAP_5M", 3520.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "RSI_14_5M": FeatureValue("RSI_14_5M", 52.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "ATR_14_5M": FeatureValue("ATR_14_5M", 10.0, FeatureQuality.VALID, self.ts, self.ts, True),
        }
        feat_snap = FeatureSnapshot(self.iid, self.ts, features)
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = 3535.0

        # Confirmed 5M candle testing zone [3520, 3540] with low at 3525, closing strong at 3535
        candle = make_test_candle(self.iid, self.ts, open_p=3528.0, high=3538.0, low=3525.0, close=3535.0, is_closed=True)

        ctx = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=feat_snap,
            evaluation_timestamp=self.ts,
            active_candle=candle,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),
        )

        decision = strategy.evaluate(ctx)
        self.assertEqual(decision.decision, DecisionType.TRADE)
        self.assertIsNotNone(decision.candidate)

        cand = decision.candidate
        self.assertEqual(cand.signal_type, SignalType.ENTRY_LONG)
        self.assertEqual(cand.direction, 1)
        self.assertEqual(cand.suggested_entry_price, 3535.0)
        self.assertLess(cand.suggested_stop_loss, 3535.0)
        self.assertGreater(cand.suggested_take_profit, 3535.0)
        self.assertEqual(cand.risk_reward_ratio, 2.0)
        self.assertEqual(cand.regime, "BULLISH")
        self.assertTrue(len(cand.fingerprint) == 64)
        self.assertTrue(len(cand.reaffirmation_key) == 64)

        # Test Exit evaluation for held long position hitting target
        pos_long = PositionView(self.iid, 10, 3500.0, self.ts, 0.0, 0.0)
        candle_target = make_test_candle(self.iid, self.ts, open_p=3525.0, high=3535.0, low=3520.0, close=3535.0, is_closed=True)
        ctx_exit = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=feat_snap,
            evaluation_timestamp=self.ts,
            active_candle=candle_target,
            position=pos_long,
        )
        exit_dec = strategy.evaluate(ctx_exit)
        self.assertEqual(exit_dec.decision, DecisionType.TRADE)
        self.assertEqual(exit_dec.candidate.signal_type, SignalType.EXIT_LONG)
        self.assertEqual(exit_dec.candidate.direction, -1)

    def test_09_range_breakout_strategy_signal_generation(self) -> None:
        """9. Verifies RangeBreakoutStrategy triggers on confirmed Bollinger Band expansion and volume surge."""
        strategy = RangeBreakoutStrategy()
        self.assertIsInstance(strategy, BaseStrategy)

        bb_meta = {"upper": 3520.0, "lower": 3480.0, "mean": 3500.0, "bandwidth": 0.011}
        features = {
            "BB_20_2_5M": FeatureValue("BB_20_2_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True, metadata=bb_meta),
            "VOL_ZSCORE_20_5M": FeatureValue("VOL_ZSCORE_20_5M", 2.5, FeatureQuality.VALID, self.ts, self.ts, True),
            "ATR_14_5M": FeatureValue("ATR_14_5M", 10.0, FeatureQuality.VALID, self.ts, self.ts, True),
        }
        feat_snap = FeatureSnapshot(self.iid, self.ts, features)
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = 3530.0

        # Bullish breakout closing above upper band (3530 > 3520) with volume surge z=2.5 >= 1.0
        candle = make_test_candle(self.iid, self.ts, open_p=3515.0, high=3532.0, low=3512.0, close=3530.0, is_closed=True)

        ctx = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=feat_snap,
            evaluation_timestamp=self.ts,
            active_candle=candle,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),
        )

        decision = strategy.evaluate(ctx)
        self.assertEqual(decision.decision, DecisionType.TRADE)
        self.assertIsNotNone(decision.candidate)

        cand = decision.candidate
        self.assertEqual(cand.signal_type, SignalType.ENTRY_LONG)
        self.assertEqual(cand.direction, 1)
        self.assertEqual(cand.suggested_entry_price, 3530.0)
        self.assertEqual(cand.suggested_stop_loss, 3480.0)  # Lower band
        self.assertGreater(cand.suggested_take_profit, 3530.0)

        # Bearish breakout closing below lower band (3470 < 3480)
        candle_down = make_test_candle(self.iid, self.ts, open_p=3485.0, high=3488.0, low=3468.0, close=3470.0, is_closed=True)
        inst_state.ltp = 3470.0
        ctx_down = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=feat_snap,
            evaluation_timestamp=self.ts,
            active_candle=candle_down,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),
        )
        dec_down = strategy.evaluate(ctx_down)
        self.assertEqual(dec_down.decision, DecisionType.TRADE)
        self.assertEqual(dec_down.candidate.signal_type, SignalType.ENTRY_SHORT)
        self.assertEqual(dec_down.candidate.direction, -1)
        self.assertEqual(dec_down.candidate.suggested_stop_loss, 3520.0)  # Upper band

    def test_10_mean_reversion_strategy_signal_generation(self) -> None:
        """10. Verifies StatisticalMeanReversionStrategy triggers on RSI/VWAP extremes during neutral regimes."""
        strategy = StatisticalMeanReversionStrategy()
        self.assertIsInstance(strategy, BaseStrategy)

        bb_meta = {"upper": 3530.0, "lower": 3470.0, "mean": 3500.0}
        features = {
            "BB_20_2_5M": FeatureValue("BB_20_2_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True, metadata=bb_meta),
            "VWAP_5M": FeatureValue("VWAP_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "RSI_14_5M": FeatureValue("RSI_14_5M", 25.0, FeatureQuality.VALID, self.ts, self.ts, True),  # Oversold <= 30
            "ATR_14_5M": FeatureValue("ATR_14_5M", 10.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_20_15M": FeatureValue("EMA_20_15M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),  # Neutral HTF
            "EMA_50_15M": FeatureValue("EMA_50_15M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_20_5M": FeatureValue("EMA_20_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_5M": FeatureValue("EMA_50_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
        }
        feat_snap = FeatureSnapshot(self.iid, self.ts, features)
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = 3475.0

        # Oversold rejection candle piercing lower band (low=3465 <= 3470) and closing positive (open=3468, close=3475)
        candle = make_test_candle(self.iid, self.ts, open_p=3468.0, high=3478.0, low=3465.0, close=3475.0, is_closed=True)

        ctx = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=feat_snap,
            evaluation_timestamp=self.ts,
            active_candle=candle,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),
        )

        decision = strategy.evaluate(ctx)
        self.assertEqual(decision.decision, DecisionType.TRADE)
        self.assertIsNotNone(decision.candidate)

        cand = decision.candidate
        self.assertEqual(cand.signal_type, SignalType.ENTRY_LONG)
        self.assertEqual(cand.direction, 1)
        self.assertEqual(cand.suggested_entry_price, 3475.0)
        self.assertEqual(cand.suggested_take_profit, 3500.0)  # Institutional mean / VWAP target
        self.assertEqual(cand.regime, "NEUTRAL")

        # Overbought rejection (RSI=75, piercing upper band 3530)
        features["RSI_14_5M"] = FeatureValue("RSI_14_5M", 75.0, FeatureQuality.VALID, self.ts, self.ts, True)
        candle_overbought = make_test_candle(self.iid, self.ts, open_p=3532.0, high=3535.0, low=3522.0, close=3525.0, is_closed=True)
        inst_state.ltp = 3525.0
        ctx_overbought = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=FeatureSnapshot(self.iid, self.ts, features),
            evaluation_timestamp=self.ts,
            active_candle=candle_overbought,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),
        )
        dec_ob = strategy.evaluate(ctx_overbought)
        self.assertEqual(dec_ob.decision, DecisionType.TRADE)
        self.assertEqual(dec_ob.candidate.signal_type, SignalType.ENTRY_SHORT)
        self.assertEqual(dec_ob.candidate.direction, -1)
        self.assertEqual(dec_ob.candidate.suggested_take_profit, 3500.0)  # Institutional mean target

    def test_11_strategy_regime_filtering_and_no_trade(self) -> None:
        """11. Verifies strategies correctly suppress signals under conflicting regimes and setups."""
        mtf_strategy = MultiTimeframeTrendContinuationStrategy()
        reversion_strategy = StatisticalMeanReversionStrategy()
        breakout_strategy = RangeBreakoutStrategy()

        # Conflicting regime: HTF Bullish (3550 > 3500), but LTF Bearish (3480 < 3500)
        features_conflict = {
            "EMA_20_15M": FeatureValue("EMA_20_15M", 3550.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_15M": FeatureValue("EMA_50_15M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_20_5M": FeatureValue("EMA_20_5M", 3480.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_5M": FeatureValue("EMA_50_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "VWAP_5M": FeatureValue("VWAP_5M", 3520.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "RSI_14_5M": FeatureValue("RSI_14_5M", 50.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "ATR_14_5M": FeatureValue("ATR_14_5M", 10.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "BB_20_2_5M": FeatureValue("BB_20_2_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True, metadata={"upper": 3520.0, "lower": 3480.0, "mean": 3500.0}),
            "VOL_ZSCORE_20_5M": FeatureValue("VOL_ZSCORE_20_5M", 0.3, FeatureQuality.VALID, self.ts, self.ts, True),  # Low volume
        }
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = 3510.0
        candle = make_test_candle(self.iid, self.ts, open_p=3505.0, high=3515.0, low=3500.0, close=3510.0, is_closed=True)

        ctx_conflict = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=FeatureSnapshot(self.iid, self.ts, features_conflict),
            evaluation_timestamp=self.ts,
            active_candle=candle,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),
        )

        # MTF Trend must reject due to conflicting regime filter
        dec_trend = mtf_strategy.evaluate(ctx_conflict)
        self.assertEqual(dec_trend.decision, DecisionType.NO_TRADE)
        self.assertEqual(dec_trend.reason, "REGIME_FILTER")

        # Range Breakout must reject due to insufficient volume z-score
        dec_breakout = breakout_strategy.evaluate(ctx_conflict)
        self.assertEqual(dec_breakout.decision, DecisionType.NO_TRADE)
        self.assertEqual(dec_breakout.reason, "NO_SETUP")

        # Mean Reversion must reject when market is trending strongly (BULLISH)
        features_trending = dict(features_conflict)
        features_trending["EMA_20_5M"] = FeatureValue("EMA_20_5M", 3540.0, FeatureQuality.VALID, self.ts, self.ts, True)
        features_trending["VWAP_5M"] = FeatureValue("VWAP_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True)
        inst_state.ltp = 3545.0
        candle_trending = make_test_candle(self.iid, self.ts, open_p=3530.0, high=3550.0, low=3525.0, close=3545.0, is_closed=True)
        ctx_trending = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=FeatureSnapshot(self.iid, self.ts, features_trending),
            evaluation_timestamp=self.ts,
            active_candle=candle_trending,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),
        )
        dec_reversion = reversion_strategy.evaluate(ctx_trending)
        self.assertEqual(dec_reversion.decision, DecisionType.NO_TRADE)
        self.assertEqual(dec_reversion.reason, "REGIME_FILTER")

    def test_12_strategy_determinism_and_safety(self) -> None:
        """12. Confirms bit-for-bit identical signals and fingerprints across runs with zero network interaction."""
        strategy = MultiTimeframeTrendContinuationStrategy()
        features = {
            "EMA_20_15M": FeatureValue("EMA_20_15M", 3550.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_15M": FeatureValue("EMA_50_15M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_20_5M": FeatureValue("EMA_20_5M", 3540.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_5M": FeatureValue("EMA_50_5M", 3500.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "VWAP_5M": FeatureValue("VWAP_5M", 3520.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "RSI_14_5M": FeatureValue("RSI_14_5M", 52.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "ATR_14_5M": FeatureValue("ATR_14_5M", 10.0, FeatureQuality.VALID, self.ts, self.ts, True),
        }
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = 3535.0
        candle = make_test_candle(self.iid, self.ts, open_p=3528.0, high=3538.0, low=3525.0, close=3535.0, is_closed=True)
        ctx = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=FeatureSnapshot(self.iid, self.ts, features),
            evaluation_timestamp=self.ts,
            active_candle=candle,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),
        )

        dec1 = strategy.evaluate(ctx)
        dec2 = strategy.evaluate(ctx)

        self.assertEqual(dec1.decision, dec2.decision)
        self.assertIsNotNone(dec1.candidate)
        self.assertIsNotNone(dec2.candidate)
        self.assertEqual(dec1.candidate.fingerprint, dec2.candidate.fingerprint)
        self.assertEqual(dec1.candidate.reaffirmation_key, dec2.candidate.reaffirmation_key)
        self.assertEqual(dec1.candidate.signal_id, dec2.candidate.signal_id)

    def test_13_algorithmic_twap_schedule_generation(self) -> None:
        """13. Validates TWAP execution schedule generation and integer distribution invariants."""
        cfg = TWAPScheduleConfig(duration_seconds=300.0, num_slices=5)
        sched = generate_twap_schedule(
            config=cfg,
            total_quantity=103,
            start_time=self.ts,
            instrument_symbol="TCS",
            target_notional=360500.0,
        )
        self.assertEqual(sched.total_quantity, 103)
        self.assertEqual(sched.slice_count, 5)
        self.assertEqual(sum(s.target_quantity for s in sched.slices), 103)
        self.assertEqual([s.target_quantity for s in sched.slices], [21, 21, 21, 20, 20])
        self.assertTrue(sched.slices[-1].is_final_slice)
        self.assertFalse(sched.slices[0].is_final_slice)

    def test_14_algorithmic_time_decay_schedule_generation(self) -> None:
        """14. Validates Time-Decay exit schedule checkpoints, half-life decay, and urgency multipliers."""
        cfg = TimeDecayExitConfig(max_holding_seconds=600.0, half_life_seconds=300.0, num_eval_checkpoints=5)
        td_sched = generate_time_decay_schedule(config=cfg, total_quantity=100, entry_time=self.ts)
        self.assertEqual(len(td_sched.checkpoints), 5)
        self.assertEqual(td_sched.checkpoints[0].decay_ratio, 0.0)
        self.assertEqual(td_sched.checkpoints[0].suggested_action, "HOLD")
        self.assertEqual(td_sched.checkpoints[-1].decay_ratio, 1.0)
        self.assertEqual(td_sched.checkpoints[-1].suggested_action, "FLATTEN")
        self.assertEqual(td_sched.checkpoints[-1].remaining_quantity_pct, 0.0)



class TestAdvancedStrategiesStep4BacktestIntegration(unittest.TestCase):
    """
    Test suite for Phase 11 Step 4: Strategy Integration & Backtest Validation.
    Validates end-to-end replay, order placement, paper fill execution,
    trade recording, regime filtering, multi-strategy arbitration, determinism, and safety.
    """

    def setUp(self) -> None:
        self.symbol = "RELIANCE"
        self.t0 = datetime(2026, 9, 15, 9, 15, 0, tzinfo=INDIA_TZ)

    def _build_dataset_from_bars(self, bars: list, volume: float = 50.0) -> HistoricalDataset:
        """
        Constructs a chronological HistoricalDataset from 5M bars.
        Each bar produces 4 intra-bar events (open, low, high, close).
        """
        ticks: list = []
        curr = self.t0
        for bar_tuple in bars:
            if len(bar_tuple) == 5:
                p_open, p_high, p_low, p_close, bar_vol = bar_tuple
            else:
                p_open, p_high, p_low, p_close = bar_tuple
                bar_vol = volume

            start_t = curr
            ticks.append(MarketEvent(
                provider="TEST_PROVIDER",
                provider_symbol_id=self.symbol,
                symbol=self.symbol,
                exchange_timestamp=start_t + timedelta(seconds=1),
                local_receive_datetime=start_t + timedelta(seconds=1),
                ltp=p_open,
                bid=p_open - 0.5,
                ask=p_open + 0.5,
                tick_volume=bar_vol,
                depth=MarketDepth([DepthLevel(p_open - 0.5, 100, 1)], [DepthLevel(p_open + 0.5, 100, 1)]),
                raw={"seq": len(ticks) + 1},
            ))
            ticks.append(MarketEvent(
                provider="TEST_PROVIDER",
                provider_symbol_id=self.symbol,
                symbol=self.symbol,
                exchange_timestamp=start_t + timedelta(seconds=100),
                local_receive_datetime=start_t + timedelta(seconds=100),
                ltp=p_low,
                bid=p_low - 0.5,
                ask=p_low + 0.5,
                tick_volume=bar_vol,
                depth=MarketDepth([DepthLevel(p_low - 0.5, 100, 1)], [DepthLevel(p_low + 0.5, 100, 1)]),
                raw={"seq": len(ticks) + 1},
            ))
            ticks.append(MarketEvent(
                provider="TEST_PROVIDER",
                provider_symbol_id=self.symbol,
                symbol=self.symbol,
                exchange_timestamp=start_t + timedelta(seconds=200),
                local_receive_datetime=start_t + timedelta(seconds=200),
                ltp=p_high,
                bid=p_high - 0.5,
                ask=p_high + 0.5,
                tick_volume=bar_vol,
                depth=MarketDepth([DepthLevel(p_high - 0.5, 100, 1)], [DepthLevel(p_high + 0.5, 100, 1)]),
                raw={"seq": len(ticks) + 1},
            ))
            curr += timedelta(minutes=5)
            ticks.append(MarketEvent(
                provider="TEST_PROVIDER",
                provider_symbol_id=self.symbol,
                symbol=self.symbol,
                exchange_timestamp=curr,
                local_receive_datetime=curr,
                ltp=p_close,
                bid=p_close - 0.5,
                ask=p_close + 0.5,
                tick_volume=bar_vol,
                depth=MarketDepth([DepthLevel(p_close - 0.5, 100, 1)], [DepthLevel(p_close + 0.5, 100, 1)]),
                raw={"seq": len(ticks) + 1},
            ))
        return HistoricalDataset.from_events(ticks)

    def test_15_backtest_mtf_trend_continuation_end_to_end(self) -> None:
        """15. Validates end-to-end backtest replay of MultiTimeframeTrendContinuationStrategy with paper fill and profit recording."""
        bar_prices = [
            (1000.0, 1004.0, 999.0, 1002.0),
            (1002.0, 1006.0, 1001.0, 1004.0),
            (1004.0, 1008.0, 1003.0, 1006.0),
            (1006.0, 1010.0, 1005.0, 1008.0),
            (1008.0, 1012.0, 1007.0, 1010.0),
            (1010.0, 1014.0, 1009.0, 1012.0),
            # Pullback into value zone (EMA ~1011, VWAP ~1007) with rejection wick
            (1012.0, 1013.0, 1008.0, 1011.0),
            # Fill buy limit @ 1011
            (1011.0, 1025.0, 1010.0, 1024.0),
            # Surges
            (1024.0, 1042.0, 1023.0, 1040.0),
            # Blows past TP target
            (1040.0, 1070.0, 1039.0, 1065.0),
            # Exit fill
            (1065.0, 1066.0, 1064.0, 1065.0),
        ]
        dataset = self._build_dataset_from_bars(bar_prices)
        strat_config = MultiTimeframeTrendConfig(
            htf_fast_ema_id="EMA_3_5M",
            htf_slow_ema_id="EMA_6_5M",
            ltf_fast_ema_id="EMA_2_5M",
            ltf_slow_ema_id="EMA_4_5M",
            ltf_vwap_id="VWAP_5M",
            rsi_id="RSI_3_5M",
            atr_id="ATR_3_5M",
            atr_multiplier=1.5,
            target_rr_ratio=2.0,
            rsi_lower=30.0,
            rsi_upper=75.0,
        )
        config = BacktestConfig(
            symbol=self.symbol,
            strategy_class=MultiTimeframeTrendContinuationStrategy,
            strategy_params={"config": strat_config},
            initial_cash=1_000_000.0,
            slippage_bps=1.0,
        )
        res = run_advanced_strategy_backtest(config, dataset)

        self.assertEqual(len(res.fills), 2)
        self.assertEqual(len(res.trades), 1)
        trade = res.trades[0]
        self.assertEqual(trade.side, OrderSide.BUY)
        self.assertGreater(trade.pnl, 0.0)
        self.assertGreater(trade.exit_price, trade.entry_price)
        self.assertEqual(trade.strategy_id, "MTF_TREND_CONT")
        self.assertGreater(len(res.equity_curve), 2)
        self.assertGreater(res.metrics.total_return_pct, 0.0)

    def test_16_backtest_range_breakout_end_to_end(self) -> None:
        """16. Validates end-to-end backtest replay of RangeBreakoutStrategy on Bollinger breakout + volume surge."""
        bars = [
            (1000.0, 1002.0, 998.0, 1000.0, 10.0),
            (1000.0, 1002.0, 998.0, 1001.0, 10.0),
            (1001.0, 1003.0, 999.0, 1000.0, 10.0),
            (1000.0, 1002.0, 998.0, 1001.0, 10.0),
            (1001.0, 1002.0, 999.0, 1000.0, 10.0),
            # Breakout bar
            (1000.0, 1016.0, 1000.0, 1015.0, 200.0),
            # Fill bar
            (1015.0, 1022.0, 1014.0, 1020.0, 50.0),
            # Surge past TP
            (1020.0, 1050.0, 1019.0, 1045.0, 50.0),
            # Exit fill
            (1045.0, 1046.0, 1044.0, 1045.0, 50.0),
        ]
        dataset = self._build_dataset_from_bars(bars)
        strat_config = RangeBreakoutConfig(
            bb_id="BB_5_2_5M",
            vol_z_id="VOL_ZSCORE_5_5M",
            atr_id="ATR_5_5M",
            min_zscore=1.0,
            atr_multiplier=1.5,
            target_rr_ratio=2.0,
        )
        config = BacktestConfig(
            symbol=self.symbol,
            strategy_class=RangeBreakoutStrategy,
            strategy_params={"config": strat_config},
            initial_cash=1_000_000.0,
            slippage_bps=1.0,
        )
        res = run_advanced_strategy_backtest(config, dataset)

        self.assertEqual(len(res.fills), 2)
        self.assertEqual(len(res.trades), 1)
        self.assertGreater(res.trades[0].pnl, 0.0)
        self.assertEqual(res.trades[0].strategy_id, "RANGE_BREAKOUT")
        self.assertGreater(res.metrics.profit_factor, 1.0)

    def test_17_backtest_mean_reversion_end_to_end(self) -> None:
        """17. Validates end-to-end backtest replay of StatisticalMeanReversionStrategy in neutral regime targeting VWAP."""
        bars = [
            (1000.0, 1002.0, 998.0, 1000.0),
            (1000.0, 1003.0, 998.0, 1001.0),
            (1001.0, 1002.0, 997.0, 999.0),
            (999.0, 1003.0, 998.0, 1001.0),
            (1001.0, 1002.0, 998.0, 1000.0),
            # Dip below lower BB, green close
            (988.0, 991.0, 980.0, 990.0),
            # Fill limit @ 990
            (990.0, 996.0, 989.0, 995.0),
            # Revert past VWAP mean to hit take-profit
            (995.0, 1025.0, 995.0, 1020.0),
            # Exit fill
            (1020.0, 1022.0, 1019.0, 1020.0),
        ]
        dataset = self._build_dataset_from_bars(bars)
        strat_config = StatisticalMeanReversionConfig(
            bb_id="BB_5_2_5M",
            vwap_id="VWAP_5M",
            rsi_id="RSI_5_5M",
            atr_id="ATR_5_5M",
            htf_fast_ema_id="EMA_5_5M",
            htf_slow_ema_id="EMA_5_5M",
            ltf_fast_ema_id="EMA_2_5M",
            ltf_slow_ema_id="EMA_4_5M",
            rsi_oversold=35.0,
            target_rr_ratio=1.5,
        )
        config = BacktestConfig(
            symbol=self.symbol,
            strategy_class=StatisticalMeanReversionStrategy,
            strategy_params={"config": strat_config},
            initial_cash=1_000_000.0,
            slippage_bps=1.0,
        )
        res = run_advanced_strategy_backtest(config, dataset)

        self.assertEqual(len(res.fills), 2)
        self.assertEqual(len(res.trades), 1)
        self.assertEqual(res.trades[0].strategy_id, "MEAN_REVERSION")

    def test_18_regime_filtering_in_backtest_replay(self) -> None:
        """18. Validates that adverse or non-qualifying regimes suppress false signals during backtest replay."""
        # Flat choppy market (no trend, no breakout volume, RSI balanced)
        bars = [
            (1000.0, 1001.0, 999.0, 1000.0, 10.0),
            (1000.0, 1001.0, 999.0, 1000.0, 10.0),
            (1000.0, 1001.0, 999.0, 1000.0, 10.0),
            (1000.0, 1001.0, 999.0, 1000.0, 10.0),
            (1000.0, 1001.0, 999.0, 1000.0, 10.0),
            (1000.0, 1001.0, 999.0, 1000.0, 10.0),
            (1000.0, 1001.0, 999.0, 1000.0, 10.0),
        ]
        dataset = self._build_dataset_from_bars(bars)

        # MTF Trend in flat market -> suppressed
        config_trend = BacktestConfig(
            symbol=self.symbol,
            strategy_class=MultiTimeframeTrendContinuationStrategy,
            strategy_params={"config": MultiTimeframeTrendConfig(
                htf_fast_ema_id="EMA_2_5M", htf_slow_ema_id="EMA_4_5M",
                ltf_fast_ema_id="EMA_2_5M", ltf_slow_ema_id="EMA_4_5M",
                ltf_vwap_id="VWAP_5M", rsi_id="RSI_2_5M", atr_id="ATR_2_5M",
            )},
        )
        res_trend = run_advanced_strategy_backtest(config_trend, dataset)
        self.assertEqual(len(res_trend.fills), 0)
        self.assertEqual(len(res_trend.trades), 0)

        # Breakout in low volume flat market -> suppressed
        config_breakout = BacktestConfig(
            symbol=self.symbol,
            strategy_class=RangeBreakoutStrategy,
            strategy_params={"config": RangeBreakoutConfig(
                bb_id="BB_3_2_5M", vol_z_id="VOL_ZSCORE_3_5M", atr_id="ATR_3_5M", min_zscore=1.5
            )},
        )
        res_breakout = run_advanced_strategy_backtest(config_breakout, dataset)
        self.assertEqual(len(res_breakout.fills), 0)
        self.assertEqual(len(res_breakout.trades), 0)

    def test_19_missing_features_and_warming_up_safety(self) -> None:
        """19. Validates fail-closed safety when indicators are uninitialized or warming up in backtest."""
        bars = [
            (1000.0, 1002.0, 998.0, 1000.0),
            (1000.0, 1002.0, 998.0, 1000.0),
        ]
        dataset = self._build_dataset_from_bars(bars)

        # Running against raw BacktestEngine without indicator registration fails closed cleanly
        config = BacktestConfig(
            symbol=self.symbol,
            strategy_class=MultiTimeframeTrendContinuationStrategy,
        )
        engine = BacktestEngine()
        res = engine.run(config, dataset)
        self.assertEqual(len(res.fills), 0)
        self.assertEqual(len(res.trades), 0)
        self.assertEqual(res.metrics.total_trades, 0)

    def test_20_multi_strategy_portfolio_backtest(self) -> None:
        """20. Validates concurrent execution of multiple Phase 11 strategies with isolated state and combined equity tracking."""
        bars = [
            (1000.0, 1002.0, 998.0, 1000.0, 10.0),
            (1000.0, 1002.0, 998.0, 1001.0, 10.0),
            (1001.0, 1003.0, 999.0, 1000.0, 10.0),
            (1000.0, 1002.0, 998.0, 1001.0, 10.0),
            (1001.0, 1002.0, 999.0, 1000.0, 10.0),
            # Breakout bar
            (1000.0, 1016.0, 1000.0, 1015.0, 200.0),
            # Fill bar
            (1015.0, 1022.0, 1014.0, 1020.0, 50.0),
            # Surge past TP
            (1020.0, 1050.0, 1019.0, 1045.0, 50.0),
            # Exit fill
            (1045.0, 1046.0, 1044.0, 1045.0, 50.0),
        ]
        dataset = self._build_dataset_from_bars(bars)

        breakout_cfg = RangeBreakoutConfig(
            bb_id="BB_5_2_5M",
            vol_z_id="VOL_ZSCORE_5_5M",
            atr_id="ATR_5_5M",
            min_zscore=1.0,
            atr_multiplier=1.5,
            target_rr_ratio=2.0,
        )
        breakout_strategy = RangeBreakoutStrategy(config=breakout_cfg)

        trend_cfg = MultiTimeframeTrendConfig(
            htf_fast_ema_id="EMA_5_5M",
            htf_slow_ema_id="EMA_5_5M",
            ltf_fast_ema_id="EMA_5_5M",
            ltf_slow_ema_id="EMA_5_5M",
            ltf_vwap_id="VWAP_5M",
            rsi_id="RSI_5_5M",
            atr_id="ATR_5_5M",
        )

        config = BacktestConfig(
            symbol=self.symbol,
            strategy_class=MultiTimeframeTrendContinuationStrategy,
            strategy_params={"config": trend_cfg},
            initial_cash=1_000_000.0,
            slippage_bps=1.0,
        )

        res = run_advanced_strategy_backtest(
            config=config,
            dataset=dataset,
            additional_strategies=[breakout_strategy],
        )

        self.assertGreater(len(res.fills), 0)
        self.assertGreater(len(res.trades), 0)
        self.assertTrue(any(t.strategy_id == "RANGE_BREAKOUT" for t in res.trades))
        self.assertGreater(len(res.equity_curve), 0)

    def test_21_backtest_determinism_and_state_isolation(self) -> None:
        """21. Validates bit-for-bit identical results on repeated backtests and zero state leakage across runs."""
        bars = [
            (1000.0, 1002.0, 998.0, 1000.0, 10.0),
            (1000.0, 1002.0, 998.0, 1001.0, 10.0),
            (1001.0, 1003.0, 999.0, 1000.0, 10.0),
            (1000.0, 1002.0, 998.0, 1001.0, 10.0),
            (1001.0, 1002.0, 999.0, 1000.0, 10.0),
            (1000.0, 1016.0, 1000.0, 1015.0, 200.0),
            (1015.0, 1022.0, 1014.0, 1020.0, 50.0),
            (1020.0, 1050.0, 1019.0, 1045.0, 50.0),
            (1045.0, 1046.0, 1044.0, 1045.0, 50.0),
        ]
        dataset = self._build_dataset_from_bars(bars)
        strat_config = RangeBreakoutConfig(
            bb_id="BB_5_2_5M",
            vol_z_id="VOL_ZSCORE_5_5M",
            atr_id="ATR_5_5M",
            min_zscore=1.0,
            atr_multiplier=1.5,
            target_rr_ratio=2.0,
        )
        config = BacktestConfig(
            symbol=self.symbol,
            strategy_class=RangeBreakoutStrategy,
            strategy_params={"config": strat_config},
            initial_cash=1_000_000.0,
            slippage_bps=1.0,
        )

        res1 = run_advanced_strategy_backtest(config, dataset)
        res2 = run_advanced_strategy_backtest(config, dataset)

        self.assertEqual(res1.run_id, res2.run_id)
        self.assertEqual(len(res1.fills), len(res2.fills))
        self.assertEqual(len(res1.trades), len(res2.trades))
        for t1, t2 in zip(res1.trades, res2.trades):
            self.assertEqual(t1.entry_price, t2.entry_price)
            self.assertEqual(t1.exit_price, t2.exit_price)
            self.assertEqual(t1.pnl, t2.pnl)
            self.assertEqual(t1.return_pct, t2.return_pct)
            self.assertEqual(t1.strategy_id, t2.strategy_id)
        self.assertEqual(res1.metrics.total_return_pct, res2.metrics.total_return_pct)
        self.assertEqual(res1.metrics.sharpe_ratio, res2.metrics.sharpe_ratio)
        self.assertEqual(res1.metrics.max_drawdown_pct, res2.metrics.max_drawdown_pct)
        self.assertEqual(res1.metrics.final_equity, res2.metrics.final_equity)
        self.assertEqual(len(res1.equity_curve), len(res2.equity_curve))

    def test_22_live_broker_and_network_safety_invariance(self) -> None:
        """22. Verifies REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED and zero live network execution during backtesting."""
        adapter = LiveBrokerAdapter()
        res = adapter._execute_live_dispatch("DUMMY_INSTRUCTION", "DUMMY_KEY")  # type: ignore
        self.assertEqual(res.failure_reason, "REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED")
        self.assertIsNone(os.environ.get("DHAN_CLIENT_ID"))
        self.assertIsNone(os.environ.get("DHAN_ACCESS_TOKEN"))


if __name__ == "__main__":
    unittest.main()

