"""
TradeGo Phase 11 — Advanced Strategies: Multi-Timeframe Composite Regime Classifier.

Provides deterministic multi-timeframe market regime classification by coordinating:
1. Higher-Timeframe (HTF, e.g. 15M/1H) trend direction (fast vs slow EMA).
2. Lower-Timeframe (LTF, e.g. 5M/1M) trend direction (fast vs slow EMA, VWAP alignment).
3. Volatility dimension (ATR in basis points relative to price).
4. Strict conflict arbitration (divergence between HTF and LTF resolves to NEUTRAL).
5. Composite hierarchical state labeling:
   - MACRO_BULLISH_LOW_VOL
   - MACRO_BULLISH_EXPANDING_VOL
   - MACRO_BULLISH
   - MACRO_BEARISH_EXPANDING_VOL
   - MACRO_BEARISH_LOW_VOL
   - MACRO_BEARISH
   - RANGING_COMPRESSED
   - NEUTRAL

Enforces:
- Full inheritance and contract compatibility with BaseRegimeClassifier.
- Zero wall-clock dependency (all evaluations use context.evaluation_timestamp).
- Zero randomness / no random numbers.
- Zero network / zero broker / zero order execution.
- Pure determinism: identical context feature inputs produce identical outputs and provenance.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from services.signals.base import BaseRegimeClassifier
from services.signals.context import StrategyContext


class MarketRegimeState(str, Enum):
    """Authoritative regime states for multi-timeframe classification."""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

    # Composite hierarchical states
    MACRO_BULLISH_LOW_VOL = "MACRO_BULLISH_LOW_VOL"
    MACRO_BULLISH_EXPANDING_VOL = "MACRO_BULLISH_EXPANDING_VOL"
    MACRO_BULLISH = "MACRO_BULLISH"
    MACRO_BEARISH_EXPANDING_VOL = "MACRO_BEARISH_EXPANDING_VOL"
    MACRO_BEARISH_LOW_VOL = "MACRO_BEARISH_LOW_VOL"
    MACRO_BEARISH = "MACRO_BEARISH"
    RANGING_COMPRESSED = "RANGING_COMPRESSED"

    # Volatility dimensions
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    NORMAL_VOLATILITY = "NORMAL_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"


@dataclass(frozen=True, slots=True)
class MultiTimeframeRegimeConfig:
    """
    Configuration parameters for multi-timeframe regime classification.
    """
    htf_fast_ema_id: str = "EMA_20_15M"
    htf_slow_ema_id: str = "EMA_50_15M"
    ltf_fast_ema_id: str = "EMA_20_5M"
    ltf_slow_ema_id: str = "EMA_50_5M"
    ltf_vwap_id: str = "VWAP_5M"
    atr_id: str = "ATR_14_5M"
    high_vol_threshold_bps: float = 80.0
    low_vol_threshold_bps: float = 20.0
    return_composite: bool = False

    def __post_init__(self) -> None:
        if self.low_vol_threshold_bps <= 0.0:
            raise ValueError(
                f"low_vol_threshold_bps must be > 0.0, got {self.low_vol_threshold_bps}"
            )
        if self.high_vol_threshold_bps <= self.low_vol_threshold_bps:
            raise ValueError(
                f"high_vol_threshold_bps ({self.high_vol_threshold_bps}) must be greater than "
                f"low_vol_threshold_bps ({self.low_vol_threshold_bps})"
            )


@dataclass(frozen=True, slots=True)
class CompositeRegimeResult:
    """
    Detailed output of multi-timeframe composite regime classification,
    including provenance, confidence, and dimensional components.
    """
    primary_regime: str
    composite_regime: str
    htf_trend: str
    ltf_trend: str
    volatility_regime: str
    atr_bps: float
    confidence: float
    timestamp: datetime
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.provenance is not None and isinstance(self.provenance, dict):
            object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))


class MultiTimeframeCompositeRegimeClassifier(BaseRegimeClassifier):
    """
    Deterministic multi-timeframe composite regime classifier.
    Combines HTF and LTF trend directions with ATR volatility into a consensus regime.
    """

    def __init__(
        self,
        config: Optional[MultiTimeframeRegimeConfig] = None,
        name: str = "MTF_COMPOSITE_REGIME",
    ) -> None:
        super().__init__(name)
        self.config = config or MultiTimeframeRegimeConfig()

    def classify(self, context: StrategyContext) -> Optional[str]:
        """
        Evaluates current market regime conforming to BaseRegimeClassifier contract.
        Returns primary regime ("BULLISH", "BEARISH", "NEUTRAL") or composite regime
        depending on config.return_composite. Returns None if data is insufficient.
        """
        detailed = self.classify_detailed(context)
        if detailed is None:
            return None
        if self.config.return_composite:
            return detailed.composite_regime
        return detailed.primary_regime

    def classify_detailed(self, context: StrategyContext) -> Optional[CompositeRegimeResult]:
        """
        Performs full multi-timeframe analysis and returns strongly-typed CompositeRegimeResult.
        """
        price = context.market_state.ltp
        if price is None and context.active_candle is not None:
            price = context.active_candle.close

        if price is None or price <= 0.0:
            return None

        # Fetch HTF features
        htf_fast = context.get_feature_value(self.config.htf_fast_ema_id)
        htf_slow = context.get_feature_value(self.config.htf_slow_ema_id)

        # Fetch LTF features
        ltf_fast = context.get_feature_value(self.config.ltf_fast_ema_id)
        ltf_slow = context.get_feature_value(self.config.ltf_slow_ema_id)
        ltf_vwap = context.get_feature_value(self.config.ltf_vwap_id)

        # Fetch Volatility feature
        atr = context.get_feature_value(self.config.atr_id)

        # Strict validation: all required features must be populated and mathematically valid
        if (
            htf_fast is None
            or htf_slow is None
            or ltf_fast is None
            or ltf_slow is None
            or ltf_vwap is None
            or atr is None
            or atr <= 0.0
        ):
            return None

        # 1. HTF Trend Evaluation
        if htf_fast > htf_slow:
            htf_trend = MarketRegimeState.BULLISH.value
        elif htf_fast < htf_slow:
            htf_trend = MarketRegimeState.BEARISH.value
        else:
            htf_trend = MarketRegimeState.NEUTRAL.value

        # 2. LTF Trend Evaluation (EMA crossover + VWAP filter)
        if ltf_fast > ltf_slow and price > ltf_vwap:
            ltf_trend = MarketRegimeState.BULLISH.value
        elif ltf_fast < ltf_slow and price < ltf_vwap:
            ltf_trend = MarketRegimeState.BEARISH.value
        else:
            ltf_trend = MarketRegimeState.NEUTRAL.value

        # 3. Trend Consensus & Conflict Precedence
        if htf_trend == MarketRegimeState.BULLISH.value and ltf_trend == MarketRegimeState.BULLISH.value:
            primary_regime = MarketRegimeState.BULLISH.value
            confidence = 1.0
        elif htf_trend == MarketRegimeState.BEARISH.value and ltf_trend == MarketRegimeState.BEARISH.value:
            primary_regime = MarketRegimeState.BEARISH.value
            confidence = 1.0
        else:
            # Conflict or lack of alignment resolves strictly to NEUTRAL
            primary_regime = MarketRegimeState.NEUTRAL.value
            confidence = 0.0

        # 4. Volatility Dimension
        atr_bps = round((atr / price) * 10000.0, 2)
        if atr_bps >= self.config.high_vol_threshold_bps:
            vol_regime = MarketRegimeState.HIGH_VOLATILITY.value
        elif atr_bps <= self.config.low_vol_threshold_bps:
            vol_regime = MarketRegimeState.LOW_VOLATILITY.value
        else:
            vol_regime = MarketRegimeState.NORMAL_VOLATILITY.value

        # 5. Composite State Mapping
        if primary_regime == MarketRegimeState.BULLISH.value:
            if vol_regime == MarketRegimeState.LOW_VOLATILITY.value:
                composite_regime = MarketRegimeState.MACRO_BULLISH_LOW_VOL.value
            elif vol_regime == MarketRegimeState.HIGH_VOLATILITY.value:
                composite_regime = MarketRegimeState.MACRO_BULLISH_EXPANDING_VOL.value
            else:
                composite_regime = MarketRegimeState.MACRO_BULLISH.value
        elif primary_regime == MarketRegimeState.BEARISH.value:
            if vol_regime == MarketRegimeState.HIGH_VOLATILITY.value:
                composite_regime = MarketRegimeState.MACRO_BEARISH_EXPANDING_VOL.value
            elif vol_regime == MarketRegimeState.LOW_VOLATILITY.value:
                composite_regime = MarketRegimeState.MACRO_BEARISH_LOW_VOL.value
            else:
                composite_regime = MarketRegimeState.MACRO_BEARISH.value
        else:
            # Neutral / Ranging
            if vol_regime == MarketRegimeState.LOW_VOLATILITY.value:
                composite_regime = MarketRegimeState.RANGING_COMPRESSED.value
            else:
                composite_regime = MarketRegimeState.NEUTRAL.value

        provenance = {
            "instrument": context.instrument_id.symbol,
            "evaluation_timestamp": context.evaluation_timestamp.isoformat(),
            "price": price,
            "htf_fast_ema": htf_fast,
            "htf_slow_ema": htf_slow,
            "htf_trend": htf_trend,
            "ltf_fast_ema": ltf_fast,
            "ltf_slow_ema": ltf_slow,
            "ltf_vwap": ltf_vwap,
            "ltf_trend": ltf_trend,
            "atr": atr,
            "atr_bps": atr_bps,
            "volatility_regime": vol_regime,
            "confidence": confidence,
        }

        return CompositeRegimeResult(
            primary_regime=primary_regime,
            composite_regime=composite_regime,
            htf_trend=htf_trend,
            ltf_trend=ltf_trend,
            volatility_regime=vol_regime,
            atr_bps=atr_bps,
            confidence=confidence,
            timestamp=context.evaluation_timestamp,
            provenance=provenance,
        )
