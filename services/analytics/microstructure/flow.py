"""
Hot-Path Order Flow Microstructure Features for Tradego Analytics (Phase 4).

Implements:
- TradeFlowImbalance: Cumulative signed volume delta with strict VolumeQuality propagation.
- TickIntensity: High-frequency rolling trade ticks per second.

Strictly O(1) arithmetic on tick ingestion hot path.
"""

from collections import deque
from datetime import datetime
from typing import Deque, Optional, Tuple

from services.candles.models import VolumeQuality
from services.market_gateway.models import MarketEvent
from services.market_state.state import InstrumentStateSnapshot
from ..base import BaseMicrostructureFeature
from ..models import FeatureQuality, FeatureValue


class TradeFlowImbalance(BaseMicrostructureFeature):
    """
    Computes cumulative signed volume delta: sum(volume * tick_direction).
    Propagates VolumeQuality:
    - COMPLETE -> VALID
    - RECONSTRUCTED / PARTIAL / ESTIMATED -> DEGRADED
    - UNAVAILABLE / Missing -> INVALID
    """

    def __init__(self, window_ticks: int = 50, name: str = "FLOW_IMBALANCE") -> None:
        super().__init__(name)
        self.window_ticks = window_ticks
        # Ring buffer storing (signed_volume, is_authoritative)
        self.buffer: Deque[Tuple[float, bool]] = deque(maxlen=window_ticks)
        self.running_signed_volume: float = 0.0
        self.degraded_count: int = 0

    def compute(
        self,
        snapshot: InstrumentStateSnapshot,
        event: Optional[MarketEvent] = None,
    ) -> FeatureValue:
        ts = snapshot.last_exchange_timestamp or snapshot.last_provider_timestamp or datetime.now()

        # Extract volume and direction from event or snapshot
        vol = 0.0
        vol_quality = VolumeQuality.COMPLETE
        direction = snapshot.tick_direction

        if event is not None:
            if event.tick_volume is not None:
                vol = event.tick_volume
            elif event.ltp_qty is not None:
                vol = float(event.ltp_qty)
                vol_quality = VolumeQuality.ESTIMATED
            else:
                vol_quality = VolumeQuality.UNAVAILABLE

        signed_vol = vol * direction
        is_auth = (vol_quality == VolumeQuality.COMPLETE)

        # Update sliding window
        if len(self.buffer) == self.window_ticks:
            evicted_vol, evicted_auth = self.buffer.popleft()
            self.running_signed_volume -= evicted_vol
            if not evicted_auth:
                self.degraded_count = max(0, self.degraded_count - 1)

        self.buffer.append((signed_vol, is_auth))
        self.running_signed_volume += signed_vol
        if not is_auth:
            self.degraded_count += 1

        if vol_quality == VolumeQuality.UNAVAILABLE or len(self.buffer) == 0:
            quality = FeatureQuality.INVALID
        elif self.degraded_count > 0:
            quality = FeatureQuality.DEGRADED
        else:
            quality = FeatureQuality.VALID

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(self.running_signed_volume, 2),
            quality=quality,
            observation_timestamp=ts,
            availability_timestamp=ts,
            is_confirmed=True,
            metadata={
                "current_tick_volume": vol,
                "tick_direction": direction,
                "window_size": len(self.buffer),
            },
        )

    def reset(self) -> None:
        self.buffer.clear()
        self.running_signed_volume = 0.0
        self.degraded_count = 0


class TickIntensity(BaseMicrostructureFeature):
    """
    Computes instantaneous trade flow intensity (trade ticks per second)
    over a rolling time window (default 5.0 seconds).
    """

    def __init__(self, window_seconds: float = 5.0, name: str = "TICK_INTENSITY") -> None:
        super().__init__(name)
        self.window_seconds = window_seconds
        # Buffer of monotonic arrival timestamps
        self.timestamps: Deque[float] = deque()

    def compute(
        self,
        snapshot: InstrumentStateSnapshot,
        event: Optional[MarketEvent] = None,
    ) -> FeatureValue:
        ts = snapshot.last_exchange_timestamp or snapshot.last_provider_timestamp or datetime.now()
        current_time = (
            event.local_receive_timestamp
            if event is not None
            else snapshot.last_local_receive_timestamp
        )

        # Evict timestamps older than current_time - window_seconds
        cutoff = current_time - self.window_seconds
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()

        self.timestamps.append(current_time)
        intensity = len(self.timestamps) / self.window_seconds

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(intensity, 2),
            quality=FeatureQuality.VALID,
            observation_timestamp=ts,
            availability_timestamp=ts,
            is_confirmed=True,
            metadata={
                "window_ticks": len(self.timestamps),
                "window_seconds": self.window_seconds,
            },
        )

    def reset(self) -> None:
        self.timestamps.clear()
