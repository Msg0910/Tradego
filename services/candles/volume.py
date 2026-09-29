"""
Volume Accounting Policies and Semantic Quality Indicators for Tradego.

Decouples volume calculation from specific market data provider protocols.
Supports both incremental per-tick volume and cumulative session volume,
with strict VolumeQuality guarantees ensuring partial volume is never
silently presented as complete authoritative exchange volume.
"""

from abc import ABC, abstractmethod
from typing import Optional, Tuple

from services.market_gateway.models import MarketEvent
from .models import ActiveCandleState, VolumeQuality


class VolumeAccountingPolicy(ABC):
    """
    Abstract contract for volume aggregation policies.
    """

    @abstractmethod
    def on_bucket_opened(self, state: ActiveCandleState, current_total_volume: Optional[float]) -> None:
        """Called when a new candle bucket begins."""
        pass

    @abstractmethod
    def compute_tick_volume(
        self, event: MarketEvent, state: ActiveCandleState
    ) -> Tuple[float, VolumeQuality]:
        """
        Computes the volume contributed by an incoming tick and determines its VolumeQuality.
        Returns: (delta_volume, VolumeQuality)
        """
        pass

    @abstractmethod
    def on_reconnect(self) -> None:
        """Notifies policy that a network feed disconnection/reconnection occurred."""
        pass


class IncrementalVolumePolicy(VolumeAccountingPolicy):
    """
    Used when the market data feed emits incremental volume per tick (e.g. tick_volume).
    """

    def on_bucket_opened(self, state: ActiveCandleState, current_total_volume: Optional[float]) -> None:
        state.volume_quality = VolumeQuality.COMPLETE

    def compute_tick_volume(
        self, event: MarketEvent, state: ActiveCandleState
    ) -> Tuple[float, VolumeQuality]:
        if event.tick_volume is not None and event.tick_volume >= 0.0:
            return event.tick_volume, VolumeQuality.COMPLETE
        if event.ltp_qty is not None and event.ltp_qty > 0:
            return float(event.ltp_qty), VolumeQuality.ESTIMATED
        return 0.0, VolumeQuality.UNAVAILABLE

    def on_reconnect(self) -> None:
        pass


class CumulativeVolumePolicy(VolumeAccountingPolicy):
    """
    Used when the market data feed emits cumulative session volume (e.g. TotalVolume).
    Calculates bucket volume as: total_volume_end - total_volume_start.
    
    Correctly handles:
    - Normal continuous sessions (COMPLETE)
    - Mid-bucket process start or late subscription (PARTIAL)
    - Feed disconnect / reconnect (RECONSTRUCTED or PARTIAL)
    - Cumulative volume reset or session rollover
    - Missing volume fields (UNAVAILABLE)
    """

    def __init__(self) -> None:
        self._last_total_volume: Optional[float] = None
        self._bucket_start_volume: Optional[float] = None
        self._is_bucket_baseline_established: bool = False
        self._reconnect_occurred: bool = False

    def on_bucket_opened(self, state: ActiveCandleState, current_total_volume: Optional[float]) -> None:
        """
        Anchors the baseline at the exact start of a new bucket.
        If current_total_volume is known, this bucket is authoritative (COMPLETE).
        """
        self._bucket_start_volume = current_total_volume
        if current_total_volume is not None:
            self._last_total_volume = current_total_volume
            self._is_bucket_baseline_established = True
        else:
            self._is_bucket_baseline_established = (self._last_total_volume is not None)
        
        if self._reconnect_occurred:
            state.volume_quality = VolumeQuality.RECONSTRUCTED
            self._reconnect_occurred = False
        elif self._is_bucket_baseline_established:
            state.volume_quality = VolumeQuality.COMPLETE
        else:
            state.volume_quality = VolumeQuality.PARTIAL

    def compute_tick_volume(
        self, event: MarketEvent, state: ActiveCandleState
    ) -> Tuple[float, VolumeQuality]:
        if event.total_volume is None:
            # Feed omitted total_volume: fallback to tick_volume if available
            if event.tick_volume is not None and event.tick_volume > 0.0:
                return event.tick_volume, VolumeQuality.PARTIAL
            return 0.0, VolumeQuality.UNAVAILABLE

        # Scenario 1: Engine started mid-bucket or subscription initiated mid-bucket
        if not self._is_bucket_baseline_established or self._bucket_start_volume is None:
            self._bucket_start_volume = event.total_volume
            self._last_total_volume = event.total_volume
            self._is_bucket_baseline_established = True
            state.volume_quality = VolumeQuality.PARTIAL
            # First tick establishes baseline; initial delta is 0
            return 0.0, VolumeQuality.PARTIAL

        # Scenario 2: Cumulative volume rollover or feed reset
        if self._last_total_volume is not None and event.total_volume < self._last_total_volume:
            # Feed reset to 0 or session rolled over
            delta = event.total_volume
            self._bucket_start_volume = 0.0
            self._last_total_volume = event.total_volume
            state.volume_quality = VolumeQuality.RECONSTRUCTED
            return max(0.0, delta), VolumeQuality.RECONSTRUCTED

        # Scenario 3: Normal continuous delta
        delta = event.total_volume - self._last_total_volume
        self._last_total_volume = event.total_volume

        # If a reconnect occurred mid-bucket, degrade quality to RECONSTRUCTED
        if self._reconnect_occurred:
            state.volume_quality = VolumeQuality.RECONSTRUCTED
            self._reconnect_occurred = False

        return max(0.0, delta), state.volume_quality

    def on_reconnect(self) -> None:
        self._reconnect_occurred = True
