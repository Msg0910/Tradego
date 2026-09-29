"""
TradeGo Phase 11 — Advanced Strategies: Statistical Mean Reversion Strategy.

Coordinates:
1. Multi-Timeframe Regime Classifier: Enforces neutral / ranging regimes (rejects strong trending regimes).
2. Extreme Rejection Setup: Detects price excursions beyond Bollinger Bands / VWAP with RSI extreme rejection.
3. Target to Mean: Projects take-profit targets back to the institutional mean (VWAP / BB centerline).
4. Position Management: Evaluates read-only PositionView for Stop-Loss and Mean-Reversion exits.
5. Deterministic Signal Generation: Emits valid, schema-compliant SignalCandidates with SHA-256 fingerprints.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from advanced_strategies.regimes.multi_timeframe import (
    MultiTimeframeCompositeRegimeClassifier,
    MultiTimeframeRegimeConfig,
)
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


@dataclass(frozen=True, slots=True)
class StatisticalMeanReversionConfig:
    """Immutable configuration for Statistical Mean Reversion Strategy."""
    bb_id: str = "BB_20_2_5M"
    vwap_id: str = "VWAP_5M"
    rsi_id: str = "RSI_14_5M"
    atr_id: str = "ATR_14_5M"
    htf_fast_ema_id: str = "EMA_20_15M"
    htf_slow_ema_id: str = "EMA_50_15M"
    ltf_fast_ema_id: str = "EMA_20_5M"
    ltf_slow_ema_id: str = "EMA_50_5M"
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    target_rr_ratio: float = 1.5
    atr_multiplier: float = 1.5
    bar_duration_seconds: float = 300.0  # 5 minutes


class StatisticalMeanReversionStrategy(BaseStrategy):
    """
    Deterministic statistical mean reversion strategy designed to trade counter-extremes
    back to the institutional mean during non-trending (neutral/ranging) regimes.
    """

    def __init__(
        self,
        config: Optional[StatisticalMeanReversionConfig] = None,
        strategy_id: str = "MEAN_REVERSION",
        strategy_version: str = "1.0.0",
        trigger_mode: TriggerMode = TriggerMode.BAR_CLOSE,
        quality_policy: QualityPolicy = QualityPolicy.STRICT,
    ) -> None:
        self.config = config or StatisticalMeanReversionConfig()
        cfg_dict = asdict(self.config)
        cfg_hash = compute_config_hash(cfg_dict)

        required_features: List[str] = [
            self.config.bb_id,
            self.config.vwap_id,
            self.config.rsi_id,
            self.config.atr_id,
            self.config.htf_fast_ema_id,
            self.config.htf_slow_ema_id,
            self.config.ltf_fast_ema_id,
            self.config.ltf_slow_ema_id,
        ]

        super().__init__(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            config_hash=cfg_hash,
            trigger_mode=trigger_mode,
            quality_policy=quality_policy,
            required_features=required_features,
        )

        regime_cfg = MultiTimeframeRegimeConfig(
            htf_fast_ema_id=self.config.htf_fast_ema_id,
            htf_slow_ema_id=self.config.htf_slow_ema_id,
            ltf_fast_ema_id=self.config.ltf_fast_ema_id,
            ltf_slow_ema_id=self.config.ltf_slow_ema_id,
            ltf_vwap_id=self.config.vwap_id,
            atr_id=self.config.atr_id,
            return_composite=False,
        )
        self.regime_classifier = MultiTimeframeCompositeRegimeClassifier(config=regime_cfg)

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
            return NO_TRADE_DEFAULT

        # 3. Classify Multi-Timeframe Regime
        # Mean reversion requires a NEUTRAL / non-trending regime
        regime = self.regime_classifier.classify(context)
        if regime != "NEUTRAL":
            return NO_TRADE_REGIME_FILTER

        # 4. Evaluate Mean Reversion Extreme Rejection Setup
        bb_fv = context.features.get(self.config.bb_id)
        if bb_fv is None or bb_fv.metadata is None:
            return NO_TRADE_NO_SETUP

        upper = bb_fv.metadata.get("upper")
        lower = bb_fv.metadata.get("lower")
        mean = bb_fv.metadata.get("mean")
        if upper is None or lower is None or mean is None:
            return NO_TRADE_NO_SETUP

        rsi = context.get_feature_value(self.config.rsi_id)
        vwap = context.get_feature_value(self.config.vwap_id)
        atr = context.get_feature_value(self.config.atr_id, default=1.0) or 1.0
        if rsi is None or vwap is None:
            return NO_TRADE_NO_SETUP

        # Oversold Rejection (Long Setup)
        if candle.low <= lower and rsi <= self.config.rsi_oversold and candle.close >= candle.open:
            entry_price = candle.close
            stop_loss = round(candle.low - (0.5 * atr), 4)
            actual_risk = max(entry_price - stop_loss, 0.05)
            # Target is the institutional mean (VWAP or Band centerline)
            take_profit = round(max(vwap, entry_price + (self.config.target_rr_ratio * actual_risk)), 4)
            rr_ratio = round((take_profit - entry_price) / actual_risk, 2)

            return self._create_signal_decision(
                context=context,
                signal_type=SignalType.ENTRY_LONG,
                direction=1,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_reward=rr_ratio,
                regime=regime,
                setup="MEAN_REVERSION_OVERSOLD",
                setup_anchor=candle.end_time,
                confidence=0.80,
            )

        # Overbought Rejection (Short Setup)
        elif candle.high >= upper and rsi >= self.config.rsi_overbought and candle.close <= candle.open:
            entry_price = candle.close
            stop_loss = round(candle.high + (0.5 * atr), 4)
            actual_risk = max(stop_loss - entry_price, 0.05)
            # Target is the institutional mean (VWAP or Band centerline)
            take_profit = round(min(vwap, entry_price - (self.config.target_rr_ratio * actual_risk)), 4)
            rr_ratio = round((entry_price - take_profit) / actual_risk, 2)

            return self._create_signal_decision(
                context=context,
                signal_type=SignalType.ENTRY_SHORT,
                direction=-1,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_reward=rr_ratio,
                regime=regime,
                setup="MEAN_REVERSION_OVERBOUGHT",
                setup_anchor=candle.end_time,
                confidence=0.80,
            )

        return NO_TRADE_NO_SETUP

    def _evaluate_exit(self, context: StrategyContext) -> StrategyDecision:
        pos = context.position
        assert pos is not None
        candle = context.active_candle
        assert candle is not None

        atr = context.get_feature_value(self.config.atr_id, default=1.0) or 1.0
        risk_dist = self.config.atr_multiplier * atr
        target_dist = risk_dist * self.config.target_rr_ratio
        current_price = candle.close

        if pos.is_long:
            sl_price = pos.entry_price - risk_dist
            tp_price = pos.entry_price + target_dist

            if current_price <= sl_price or current_price >= tp_price:
                return self._create_signal_decision(
                    context=context,
                    signal_type=SignalType.EXIT_LONG,
                    direction=-1,
                    entry_price=current_price,
                    stop_loss=None,
                    take_profit=None,
                    risk_reward=None,
                    regime="EXIT",
                    setup="STOP_OR_TARGET",
                    setup_anchor=context.evaluation_timestamp,
                    confidence=1.0,
                )

        elif pos.is_short:
            sl_price = pos.entry_price + risk_dist
            tp_price = pos.entry_price - target_dist

            if current_price >= sl_price or current_price <= tp_price:
                return self._create_signal_decision(
                    context=context,
                    signal_type=SignalType.EXIT_SHORT,
                    direction=1,
                    entry_price=current_price,
                    stop_loss=None,
                    take_profit=None,
                    risk_reward=None,
                    regime="EXIT",
                    setup="STOP_OR_TARGET",
                    setup_anchor=context.evaluation_timestamp,
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

        final_confidence = confidence
        if self.has_degraded_features(context):
            final_confidence = round(final_confidence * 0.70, 4)

        market_ts = candle.end_time
        availability_ts = candle.end_time
        gen_ts = context.evaluation_timestamp

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

        signal_id = f"sig_{self.strategy_id}_{context.instrument_id.symbol}_{int(market_ts.timestamp())}_{direction}_{fingerprint[:8]}"

        candidate = SignalCandidate(
            signal_id=signal_id,
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
