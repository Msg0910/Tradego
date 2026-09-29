"""
CandleBuilder: Single-Timeframe OHLCV/OI Candle Aggregator for Tradego.

Manages active bar construction, boundary transitions, non-destructive updates,
late tick rejection, and explicit clock-assisted finalization.
"""

from datetime import datetime
from typing import Any, Optional, Tuple

from services.market_gateway.models import MarketEvent
from services.market_state.instrument import InstrumentId
from .calendar import ExchangeCalendar
from .models import ActiveCandleState, Candle, VolumeQuality
from .timeframe import TimeFrame
from .volume import VolumeAccountingPolicy


class CandleBuilder:
    """
    Manages the active forming candle and boundary finalization for a single
    (InstrumentId, TimeFrame) pair.
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        timeframe: TimeFrame,
        calendar: ExchangeCalendar,
        volume_policy: VolumeAccountingPolicy,
    ) -> None:
        self.instrument_id = instrument_id
        self.timeframe = timeframe
        self.calendar = calendar
        self.volume_policy = volume_policy

        self.state: Optional[ActiveCandleState] = None
        self.last_finalized_candle: Optional[Candle] = None

        # Diagnostics & Accounting
        self.total_ticks_processed: int = 0
        self.late_ticks_rejected: int = 0
        self.duplicate_ticks_ignored: int = 0

        # Duplicate detection cache: (timestamp, ltp, total_volume)
        self._last_tick_fingerprint: Optional[Tuple[Any, ...]] = None

    @property
    def is_active(self) -> bool:
        """True if an active candle is currently open and initialized."""
        return self.state is not None and self.state.is_initialized

    def _resolve_market_time(self, event: MarketEvent) -> datetime:
        """
        Market Time Hierarchy (Strict Invariant):
        1. exchange_timestamp
        2. provider_timestamp
        3. local_receive_datetime
        """
        if event.exchange_timestamp is not None:
            return event.exchange_timestamp
        if event.provider_timestamp is not None:
            return event.provider_timestamp
        if event.local_receive_datetime is not None:
            return event.local_receive_datetime
        # Fallback to current UTC if all are missing
        return datetime.now(self.calendar.resolve_session(self.instrument_id, datetime.now()).timezone)

    def process_tick(self, event: MarketEvent) -> Optional[Candle]:
        """
        Ingests a normalized MarketEvent on the critical tick path.

        Returns:
            Finalized Candle if this tick caused a bucket boundary transition,
            None otherwise.
        """
        # Quote-only event with no trade price: do not create or alter OHLC
        if event.ltp is None:
            return None

        # Duplicate tick detection
        fingerprint = (
            event.exchange_timestamp or event.provider_timestamp,
            event.ltp,
            event.total_volume,
            event.tick_volume,
            event.oi,
        )
        if self._last_tick_fingerprint is not None and fingerprint == self._last_tick_fingerprint:
            self.duplicate_ticks_ignored += 1
            return None
        self._last_tick_fingerprint = fingerprint

        market_dt = self._resolve_market_time(event)
        session = self.calendar.resolve_session(self.instrument_id, market_dt)

        # Case 1: First tick ever for this builder
        if self.state is None or not self.state.is_initialized:
            b_start, b_end = self.calendar.get_bucket_bounds(market_dt, self.timeframe, self.instrument_id)
            self._open_new_bucket(b_start, b_end, event, market_dt, session.session_id)
            return None

        # Case 2: Boundary Transition (market_dt >= current bucket end)
        if market_dt >= self.state.end_time:
            finalized = self._finalize_current_bucket()

            # Open new bucket for incoming tick
            b_start, b_end = self.calendar.get_bucket_bounds(market_dt, self.timeframe, self.instrument_id)
            self._open_new_bucket(b_start, b_end, event, market_dt, session.session_id)
            return finalized

        # Case 3: Late Tick for an already finalized bucket
        if market_dt < self.state.start_time:
            self.late_ticks_rejected += 1
            # Invariant: Confirmed historical bars are immutable; reject from active stream
            return None

        # Case 4: Normal intra-bucket tick
        self._update_active_candle(event, market_dt)
        return None

    def _open_new_bucket(
        self,
        start_time: datetime,
        end_time: datetime,
        event: MarketEvent,
        market_dt: datetime,
        session_id: str,
    ) -> None:
        """Initializes a new active candle bucket."""
        ltp = float(event.ltp)
        self.state = ActiveCandleState(
            instrument_id=self.instrument_id,
            timeframe=self.timeframe,
            start_time=start_time,
            end_time=end_time,
            open=ltp,
            high=ltp,
            low=ltp,
            close=ltp,
            volume=0.0,
            ticks=0,
            volume_quality=VolumeQuality.COMPLETE,
            open_oi=event.oi,
            high_oi=event.oi,
            low_oi=event.oi,
            close_oi=event.oi,
            sum_pv=0.0,
            is_initialized=True,
            session_id=session_id,
        )

        # Notify volume policy of new bucket start
        self.volume_policy.on_bucket_opened(self.state, event.total_volume)

        # Apply initial tick volume
        delta_vol, quality = self.volume_policy.compute_tick_volume(event, self.state)
        self.state.volume = delta_vol
        self.state.sum_pv = ltp * delta_vol
        self.state.volume_quality = quality
        self.state.ticks = 1
        self.total_ticks_processed += 1

    def _update_active_candle(self, event: MarketEvent, market_dt: datetime) -> None:
        """Applies a tick update to the currently open active candle."""
        assert self.state is not None
        ltp = float(event.ltp)

        # OHLC Ratcheting
        if ltp > self.state.high:
            self.state.high = ltp
        if ltp < self.state.low:
            self.state.low = ltp
        self.state.close = ltp

        # Volume Accounting
        delta_vol, quality = self.volume_policy.compute_tick_volume(event, self.state)
        self.state.volume += delta_vol
        self.state.sum_pv += ltp * delta_vol

        # Degrade volume quality if any tick was partial
        if quality != VolumeQuality.COMPLETE:
            self.state.volume_quality = quality

        # Open Interest
        if event.oi is not None:
            if self.state.open_oi is None:
                self.state.open_oi = event.oi
            if self.state.high_oi is None or event.oi > self.state.high_oi:
                self.state.high_oi = event.oi
            if self.state.low_oi is None or event.oi < self.state.low_oi:
                self.state.low_oi = event.oi
            self.state.close_oi = event.oi

        self.state.ticks += 1
        self.total_ticks_processed += 1

    def _finalize_current_bucket(self) -> Candle:
        """Seals the active state into an immutable finalized Candle."""
        assert self.state is not None
        candle = self.state.to_candle(is_closed=True)
        self.last_finalized_candle = candle
        self.state.is_initialized = False
        return candle

    def finalize_if_due(self, market_time: datetime) -> Optional[Candle]:
        """
        Clock-assisted coordinator finalization.
        Closes the active bucket if market_time >= active.end_time.
        Does NOT create a synthetic new bucket if no new ticks arrived.
        """
        if self.state is not None and self.state.is_initialized:
            if market_time >= self.state.end_time:
                return self._finalize_current_bucket()
        return None

    def force_finalize(self) -> Optional[Candle]:
        """
        Forced finalization (e.g. at session close or process shutdown).
        """
        if self.state is not None and self.state.is_initialized:
            return self._finalize_current_bucket()
        return None

    def get_active_candle(self) -> Optional[Candle]:
        """Returns a point-in-time immutable snapshot of the active forming candle."""
        if self.state is not None and self.state.is_initialized:
            return self.state.to_candle(is_closed=False)
        return None
