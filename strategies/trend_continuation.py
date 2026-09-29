"""
Deterministic Reference Strategy: EMA/VWAP Trend Continuation (Phase 5).

Implements the single approved reference strategy:
- Universe: Equity / Index Futures (NIFTY, BANKNIFTY, Liquid Large-Caps)
- Timeframe: 5M
- Entry Trigger: BAR_CLOSE
- Regime: EMABasedTrendRegime (EMA20 > EMA50 and LTP > VWAP for Bullish; < for Bearish)
- Setup: EMAVWAPPullbackSetup (Pullback to zone between EMA20 and VWAP; 40 <= RSI <= 60)
- Trigger: Confirmed 5M candle with rejection wick in macro-trend direction
- Exit: Geometry evaluation via read-only PositionView (SL: 1.5 * ATR14; TP: 2.0 * RR)
- Generates: SignalCandidate (ENTRY_LONG, ENTRY_SHORT, EXIT_LONG, EXIT_SHORT)
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Optional
import uuid

from services.signals.base import BaseStrategy
from services.signals.context import StrategyContext
from services.signals.fingerprint import (
    compute_config_hash,
    compute_reaffirmation_key,
    compute_signal_fingerprint,
)
from services.signals.models import (
    NO_TRADE_DEFAULT,
    NO_TRADE_NO_SETUP,
    NO_TRADE_REGIME_FILTER,
    DecisionType,
    QualityPolicy,
    SignalCandidate,
    SignalType,
    StrategyDecision,
    TriggerMode,
)
from services.signals.regimes.trend import EMABasedTrendRegime
from services.signals.setups.pullback import EMAVWAPPullbackSetup


@dataclass(frozen=True, slots=True)
class TrendContinuationConfig:
    """Immutable configuration for EMA/VWAP Trend Continuation Strategy."""
    fast_ema_id: str = "EMA_20_5M"
    slow_ema_id: str = "EMA_50_5M"
    vwap_id: str = "VWAP_5M"
    rsi_id: str = "RSI_14_5M"
    atr_id: str = "ATR_14_5M"
    atr_multiplier: float = 1.5
    target_rr_ratio: float = 2.0
    rsi_lower: float = 40.0
    rsi_upper: float = 60.0
    bar_duration_seconds: float = 300.0  # 5 minutes


class EMA_VWAP_TrendContinuationStrategy(BaseStrategy):
    """
    Deterministic reference strategy evaluating trend continuation pullbacks on 5M bars.
    """

    def __init__(
        self,
        config: Optional[TrendContinuationConfig] = None,
        strategy_id: str = "EMA_VWAP_TREND_CONT",
        strategy_version: str = "1.0.0",
        trigger_mode: TriggerMode = TriggerMode.BAR_CLOSE,
        quality_policy: QualityPolicy = QualityPolicy.STRICT,
    ) -> None:
        self.config = config or TrendContinuationConfig()
        cfg_dict = asdict(self.config)
        cfg_hash = compute_config_hash(cfg_dict)

        required_features = [
            self.config.fast_ema_id,
            self.config.slow_ema_id,
            self.config.vwap_id,
            self.config.rsi_id,
            self.config.atr_id,
        ]

        super().__init__(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            config_hash=cfg_hash,
            trigger_mode=trigger_mode,
            quality_policy=quality_policy,
            required_features=required_features,
        )

        self.regime_classifier = EMABasedTrendRegime(
            fast_ema_id=self.config.fast_ema_id,
            slow_ema_id=self.config.slow_ema_id,
            vwap_id=self.config.vwap_id,
        )

        self.setup_detector = EMAVWAPPullbackSetup(
            ema_id=self.config.fast_ema_id,
            vwap_id=self.config.vwap_id,
            rsi_id=self.config.rsi_id,
            rsi_lower=self.config.rsi_lower,
            rsi_upper=self.config.rsi_upper,
        )

    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        # 1. Validate Feature Quality and TriggerMode invariants
        invalid_decision = self.validate_features_and_trigger_mode(context)
        if invalid_decision is not None:
            return invalid_decision

        candle = context.active_candle
        if candle is None:
            return NO_TRADE_DEFAULT

        # 2. Check Exits first if position is currently held (via read-only PositionView)
        if context.position is not None and not context.position.is_flat:
            exit_decision = self._evaluate_exit(context)
            if exit_decision.decision == DecisionType.TRADE:
                return exit_decision

        # If already in position, do not evaluate new entries
        if context.position is not None and not context.position.is_flat:
            return NO_TRADE_DEFAULT

        # 3. Classify Regime (Bullish vs Bearish)
        regime = self.regime_classifier.classify(context)
        if regime not in ("BULLISH", "BEARISH"):
            return NO_TRADE_REGIME_FILTER

        # 4. Evaluate Pullback Setup
        setup_result = self.setup_detector.evaluate(context, regime=regime)
        if not setup_result.is_active:
            return NO_TRADE_NO_SETUP

        # 5. Evaluate Rejection Wick / Bar Trigger in macro-trend direction
        atr = context.get_feature_value(self.config.atr_id, default=1.0) or 1.0
        risk_dist = self.config.atr_multiplier * atr

        if regime == "BULLISH" and setup_result.direction == 1:
            # Rejection wick off lows: close in upper half of candle
            candle_range = candle.high - candle.low
            if candle_range > 0 and (candle.close >= candle.low + 0.4 * candle_range):
                entry_price = candle.close
                stop_loss = round(min(candle.low, entry_price - risk_dist), 4)
                actual_risk = max(entry_price - stop_loss, 0.05)
                take_profit = round(entry_price + (self.config.target_rr_ratio * actual_risk), 4)

                return self._create_signal_decision(
                    context=context,
                    signal_type=SignalType.ENTRY_LONG,
                    direction=1,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    risk_reward=self.config.target_rr_ratio,
                    regime=regime,
                    setup=self.setup_detector.setup_name,
                    setup_anchor=setup_result.setup_anchor_timestamp,
                    confidence=setup_result.confidence,
                )

        elif regime == "BEARISH" and setup_result.direction == -1:
            # Rejection wick off highs: close in lower half of candle
            candle_range = candle.high - candle.low
            if candle_range > 0 and (candle.close <= candle.high - 0.4 * candle_range):
                entry_price = candle.close
                stop_loss = round(max(candle.high, entry_price + risk_dist), 4)
                actual_risk = max(stop_loss - entry_price, 0.05)
                take_profit = round(entry_price - (self.config.target_rr_ratio * actual_risk), 4)

                return self._create_signal_decision(
                    context=context,
                    signal_type=SignalType.ENTRY_SHORT,
                    direction=-1,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    risk_reward=self.config.target_rr_ratio,
                    regime=regime,
                    setup=self.setup_detector.setup_name,
                    setup_anchor=setup_result.setup_anchor_timestamp,
                    confidence=setup_result.confidence,
                )

        return NO_TRADE_DEFAULT

    def _evaluate_exit(self, context: StrategyContext) -> StrategyDecision:
        """Evaluates exit geometry using read-only PositionView."""
        pos = context.position
        assert pos is not None
        candle = context.active_candle
        if candle is None:
            return NO_TRADE_DEFAULT

        atr = context.get_feature_value(self.config.atr_id, default=1.0) or 1.0
        risk_dist = self.config.atr_multiplier * atr
        target_dist = self.config.target_rr_ratio * risk_dist

        if pos.is_long:
            # Long Exit Check: Stop Loss or Target hit
            sl_price = pos.entry_price - risk_dist
            tp_price = pos.entry_price + target_dist

            is_stop_hit = candle.low <= sl_price
            is_target_hit = candle.high >= tp_price

            if is_stop_hit or is_target_hit:
                exit_price = sl_price if is_stop_hit else tp_price
                return self._create_signal_decision(
                    context=context,
                    signal_type=SignalType.EXIT_LONG,
                    direction=-1,
                    entry_price=exit_price,
                    stop_loss=None,
                    take_profit=None,
                    risk_reward=None,
                    regime="EXIT",
                    setup="STOP_OR_TARGET_EXIT",
                    setup_anchor=pos.entry_time,
                    confidence=1.0,
                )

        elif pos.is_short:
            # Short Exit Check: Stop Loss or Target hit
            sl_price = pos.entry_price + risk_dist
            tp_price = pos.entry_price - target_dist

            is_stop_hit = candle.high >= sl_price
            is_target_hit = candle.low <= tp_price

            if is_stop_hit or is_target_hit:
                exit_price = sl_price if is_stop_hit else tp_price
                return self._create_signal_decision(
                    context=context,
                    signal_type=SignalType.EXIT_SHORT,
                    direction=1,
                    entry_price=exit_price,
                    stop_loss=None,
                    take_profit=None,
                    risk_reward=None,
                    regime="EXIT",
                    setup="STOP_OR_TARGET_EXIT",
                    setup_anchor=pos.entry_time,
                    confidence=1.0,
                )

        return NO_TRADE_DEFAULT

    def _create_signal_decision(
        self,
        context: StrategyContext,
        signal_type: SignalType,
        direction: int,
        entry_price: Optional[float],
        stop_loss: Optional[float],
        take_profit: Optional[float],
        risk_reward: Optional[float],
        regime: str,
        setup: str,
        setup_anchor: datetime,
        confidence: float,
    ) -> StrategyDecision:
        candle = context.active_candle
        assert candle is not None

        # Base confidence with degraded penalty if applicable
        final_confidence = confidence
        if self.has_degraded_features(context):
            final_confidence = round(final_confidence * 0.70, 4)

        market_ts = candle.end_time
        availability_ts = candle.end_time
        gen_ts = context.evaluation_timestamp

        # Ensure generated_timestamp satisfies causality
        if gen_ts < availability_ts:
            gen_ts = availability_ts

        expiry_ts = gen_ts + timedelta(seconds=self.config.bar_duration_seconds)

        fingerprint = compute_signal_fingerprint(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            config_hash=self.config_hash,
            instrument_id=context.instrument_id,
            signal_type=signal_type.value,
            direction=direction,
            trigger_mode=self.trigger_mode.value,
            market_timestamp=market_ts,
            availability_timestamp=availability_ts,
            suggested_entry_price=entry_price,
            suggested_stop_loss=stop_loss,
            suggested_take_profit=take_profit,
            regime=regime,
            setup=setup,
        )

        reaffirmation_key = compute_reaffirmation_key(
            strategy_id=self.strategy_id,
            instrument_id=context.instrument_id,
            setup=setup,
            direction=direction,
            setup_anchor_timestamp=setup_anchor,
        )

        candidate = SignalCandidate(
            signal_id=str(uuid.uuid4()),
            fingerprint=fingerprint,
            reaffirmation_key=reaffirmation_key,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            config_hash=self.config_hash,
            instrument_id=context.instrument_id,
            signal_type=signal_type,
            direction=direction,
            trigger_mode=self.trigger_mode,
            confidence_score=final_confidence,
            suggested_entry_price=entry_price,
            suggested_stop_loss=stop_loss,
            suggested_take_profit=take_profit,
            risk_reward_ratio=risk_reward,
            priority=0,
            market_timestamp=market_ts,
            availability_timestamp=availability_ts,
            generated_timestamp=gen_ts,
            expiry_timestamp=expiry_ts,
            time_in_force_seconds=self.config.bar_duration_seconds,
            is_confirmed=(self.trigger_mode == TriggerMode.BAR_CLOSE),
            regime=regime,
            setup=setup,
            metadata={"setup_anchor": setup_anchor.isoformat()},
        )

        return StrategyDecision(decision=DecisionType.TRADE, candidate=candidate)
