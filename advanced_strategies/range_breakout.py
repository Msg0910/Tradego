"""
TradeGo Phase 11 — Advanced Strategies: Range Breakout Strategy.

Coordinates:
1. Range Breakout Setup: Detects price closing outside Bollinger Bands with Volume Z-Score surge.
2. Geometry Formulation: Calculates Stop-Loss based on opposite band / ATR and Take-Profit with configured R:R.
3. Position Management: Evaluates read-only PositionView for Stop-Loss and Take-Profit exits.
4. Deterministic Signal Generation: Emits valid, schema-compliant SignalCandidates with SHA-256 fingerprints.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import List, Optional

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
    DecisionType,
    QualityPolicy,
    SignalCandidate,
    SignalType,
    StrategyDecision,
    TriggerMode,
)
from services.signals.setups.breakout import RangeBreakoutSetup


@dataclass(frozen=True, slots=True)
class RangeBreakoutConfig:
    """Immutable configuration for Range Breakout Strategy."""
    bb_id: str = "BB_20_2_5M"
    vol_z_id: str = "VOL_ZSCORE_20_5M"
    atr_id: str = "ATR_14_5M"
    min_zscore: float = 1.0
    target_rr_ratio: float = 2.0
    atr_multiplier: float = 1.5
    bar_duration_seconds: float = 300.0  # 5 minutes


class RangeBreakoutStrategy(BaseStrategy):
    """
    Deterministic volatility expansion strategy detecting confirmed candle closes
    beyond Bollinger Bands with volume surge confirmation.
    """

    def __init__(
        self,
        config: Optional[RangeBreakoutConfig] = None,
        strategy_id: str = "RANGE_BREAKOUT",
        strategy_version: str = "1.0.0",
        trigger_mode: TriggerMode = TriggerMode.BAR_CLOSE,
        quality_policy: QualityPolicy = QualityPolicy.STRICT,
    ) -> None:
        self.config = config or RangeBreakoutConfig()
        cfg_dict = asdict(self.config)
        cfg_hash = compute_config_hash(cfg_dict)

        required_features: List[str] = [
            self.config.bb_id,
            self.config.vol_z_id,
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

        self.setup_detector = RangeBreakoutSetup(
            bb_id=self.config.bb_id,
            vol_z_id=self.config.vol_z_id,
            min_zscore=self.config.min_zscore,
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
            return NO_TRADE_DEFAULT

        # 3. Evaluate Range Breakout Setup
        setup_result = self.setup_detector.evaluate(context)
        if not setup_result.is_active:
            return NO_TRADE_NO_SETUP

        atr = context.get_feature_value(self.config.atr_id, default=1.0) or 1.0
        risk_dist = self.config.atr_multiplier * atr

        entry_price = setup_result.suggested_entry_price or candle.close
        stop_loss = setup_result.suggested_stop_loss
        take_profit = setup_result.suggested_take_profit

        if setup_result.direction == 1:
            if stop_loss is None:
                stop_loss = round(entry_price - risk_dist, 4)
            actual_risk = max(entry_price - stop_loss, 0.05)
            if take_profit is None:
                take_profit = round(entry_price + (self.config.target_rr_ratio * actual_risk), 4)

            return self._create_signal_decision(
                context=context,
                signal_type=SignalType.ENTRY_LONG,
                direction=1,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_reward=self.config.target_rr_ratio,
                regime="BREAKOUT",
                setup=self.setup_detector.setup_name,
                setup_anchor=setup_result.setup_anchor_timestamp,
                confidence=setup_result.confidence,
            )

        elif setup_result.direction == -1:
            if stop_loss is None:
                stop_loss = round(entry_price + risk_dist, 4)
            actual_risk = max(stop_loss - entry_price, 0.05)
            if take_profit is None:
                take_profit = round(entry_price - (self.config.target_rr_ratio * actual_risk), 4)

            return self._create_signal_decision(
                context=context,
                signal_type=SignalType.ENTRY_SHORT,
                direction=-1,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_reward=self.config.target_rr_ratio,
                regime="BREAKOUT",
                setup=self.setup_detector.setup_name,
                setup_anchor=setup_result.setup_anchor_timestamp,
                confidence=setup_result.confidence,
            )

        return NO_TRADE_DEFAULT

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
