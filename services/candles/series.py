"""
InstrumentCandleSeries: Multi-Timeframe Candle Management for a Single Instrument.

Implements the two-path architecture:
- Path A (Closed Path): Cascades closed 1M bars into higher-timeframe aggregators (3M..1D).
  Higher timeframe builders NEVER receive raw ticks on the hot path.
- Path B (Active Preview Path): Dynamically rolls up closed 1M bars in the current bucket
  with the live forming 1M bar to provide instant sub-microsecond active previews.

Maintains bounded in-memory ring buffers per timeframe.
"""

import threading
from collections import deque
from datetime import datetime
from typing import Callable, Deque, Dict, List, Optional

from services.market_gateway.models import MarketEvent
from services.market_state.instrument import InstrumentId
from .builder import CandleBuilder
from .calendar import ExchangeCalendar
from .models import Candle, VolumeQuality
from .timeframe import (
    TF_1D,
    TF_1H,
    TF_1M,
    TF_1S,
    TF_3M,
    TF_5M,
    TF_15M,
    TF_30M,
    TimeFrame,
)
from .volume import VolumeAccountingPolicy

# Standard History Capacities (Ring Buffer Max Lengths)
DEFAULT_RING_BUFFER_CAPACITIES: Dict[TimeFrame, int] = {
    TF_1S: 3600,   # 1 hour of 1-second bars
    TF_1M: 1440,   # 1 full 24h day of 1-minute bars
    TF_3M: 1000,   # ~50 hours
    TF_5M: 1000,   # ~83 hours (~10 trading days)
    TF_15M: 1000,  # ~1 month
    TF_30M: 1000,  # ~2 months
    TF_1H: 1000,   # ~6 months
    TF_1D: 1000,   # ~4 years
}


class InstrumentCandleSeries:
    """
    Thread-safe container managing multi-timeframe candle generation and bounded
    historical ring buffers for one instrument.
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        calendar: ExchangeCalendar,
        volume_policy: VolumeAccountingPolicy,
        capacities: Optional[Dict[TimeFrame, int]] = None,
    ) -> None:
        self.instrument_id = instrument_id
        self.calendar = calendar
        self.volume_policy = volume_policy
        self._lock = threading.Lock()

        caps = dict(DEFAULT_RING_BUFFER_CAPACITIES)
        if capacities:
            caps.update(capacities)
        self._capacities = caps

        # Base Granular Builders (receiving raw ticks on hot path)
        self._builder_1s = CandleBuilder(instrument_id, TF_1S, calendar, volume_policy)
        self._builder_1m = CandleBuilder(instrument_id, TF_1M, calendar, volume_policy)

        # Higher Timeframes (cascaded from closed 1M bars; NEVER receive raw ticks)
        self._higher_timeframes = [TF_3M, TF_5M, TF_15M, TF_30M, TF_1H, TF_1D]

        # Bounded Ring Buffers for Closed Historical Bars
        self._history: Dict[TimeFrame, Deque[Candle]] = {
            tf: deque(maxlen=caps[tf]) for tf in [TF_1S, TF_1M] + self._higher_timeframes
        }

        # Staging accumulator of closed 1M bars for the active higher-timeframe buckets
        # key: TimeFrame -> list of closed 1M candles within current bucket
        self._staging_1m_for_higher_tf: Dict[TimeFrame, List[Candle]] = {
            tf: [] for tf in self._higher_timeframes
        }

    # =========================================================================
    # PATH A: CLOSED / HISTORICAL PATH (Event-Driven Cascade)
    # =========================================================================

    def process_tick(self, event: MarketEvent) -> List[Candle]:
        """
        Hot path ingestion method.
        Updates base builders (1S, 1M). Cascades closed bars into higher timeframes.
        Returns a list of all candles finalized by this tick across timeframes.
        """
        closed_candles: List[Candle] = []

        with self._lock:
            # 1. Update 1S Base Builder
            c_1s = self._builder_1s.process_tick(event)
            if c_1s is not None:
                self._history[TF_1S].append(c_1s)
                closed_candles.append(c_1s)

            # 2. Update 1M Base Builder
            c_1m = self._builder_1m.process_tick(event)
            if c_1m is not None:
                self._history[TF_1M].append(c_1m)
                closed_candles.append(c_1m)

                # 3. Cascade closed 1M bar into higher timeframes
                higher_closed = self._cascade_closed_1m_bar(c_1m)
                closed_candles.extend(higher_closed)

        return closed_candles

    def _cascade_closed_1m_bar(self, bar_1m: Candle) -> List[Candle]:
        """
        Cascades a finalized 1M bar into higher timeframe aggregators.
        Internal helper: must be called with self._lock held.
        """
        closed_higher: List[Candle] = []

        for tf in self._higher_timeframes:
            staging = self._staging_1m_for_higher_tf[tf]
            b_start, b_end = self.calendar.get_bucket_bounds(bar_1m.start_time, tf, self.instrument_id)

            # Check if this 1M bar belongs to a new bucket compared to staging
            if staging:
                current_bucket_start = staging[0].start_time
                st_b_start, st_b_end = self.calendar.get_bucket_bounds(current_bucket_start, tf, self.instrument_id)

                if bar_1m.start_time >= st_b_end:
                    # Finalize the previous higher-timeframe bucket from accumulated 1M bars!
                    finalized_bar = self._rollup_closed_bars(staging, tf, st_b_start, st_b_end)
                    self._history[tf].append(finalized_bar)
                    closed_higher.append(finalized_bar)
                    staging.clear()

            staging.append(bar_1m)

            # If the bar exactly hits bucket end, check if bucket is complete
            b_start_cur, b_end_cur = self.calendar.get_bucket_bounds(bar_1m.start_time, tf, self.instrument_id)
            if bar_1m.end_time >= b_end_cur and staging:
                finalized_bar = self._rollup_closed_bars(staging, tf, b_start_cur, b_end_cur)
                self._history[tf].append(finalized_bar)
                closed_higher.append(finalized_bar)
                staging.clear()

        return closed_higher

    def _rollup_closed_bars(
        self, bars: List[Candle], tf: TimeFrame, start_time: datetime, end_time: datetime
    ) -> Candle:
        """Rolls up a list of contiguous closed bars into a single finalized higher-TF Candle."""
        assert bars, "Cannot rollup empty list of bars"
        open_price = bars[0].open
        high_price = max(b.high for b in bars)
        low_price = min(b.low for b in bars)
        close_price = bars[-1].close
        total_vol = sum(b.volume for b in bars)
        total_ticks = sum(b.ticks for b in bars)

        # Volume quality degrades if any component bar was not complete
        v_quality = VolumeQuality.COMPLETE
        for b in bars:
            if b.volume_quality != VolumeQuality.COMPLETE:
                v_quality = b.volume_quality
                break

        # OI
        open_oi = bars[0].open_oi
        valid_high_ois = [b.high_oi for b in bars if b.high_oi is not None]
        high_oi = max(valid_high_ois) if valid_high_ois else None
        valid_low_ois = [b.low_oi for b in bars if b.low_oi is not None]
        low_oi = min(valid_low_ois) if valid_low_ois else None
        close_oi = bars[-1].close_oi

        # VWAP
        sum_pv = sum(b.vwap * b.volume for b in bars if b.vwap is not None and b.volume > 0)
        vwap = (sum_pv / total_vol) if total_vol > 0 else close_price

        return Candle(
            instrument_id=self.instrument_id,
            timeframe=tf,
            start_time=start_time,
            end_time=end_time,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=total_vol,
            ticks=total_ticks,
            volume_quality=v_quality,
            open_oi=open_oi,
            high_oi=high_oi,
            low_oi=low_oi,
            close_oi=close_oi,
            vwap=round(vwap, 4),
            is_closed=True,
            session_id=bars[0].session_id,
        )

    # =========================================================================
    # PATH B: ACTIVE PREVIEW PATH (On-Demand Dynamic Rollup)
    # =========================================================================

    def get_active_candle(self, timeframe: TimeFrame) -> Optional[Candle]:
        """
        Returns real-time point-in-time active forming candle.
        Sub-microsecond on-demand evaluation: rolls up closed 1M bars in the current
        higher timeframe window with the live forming 1M bar.
        """
        with self._lock:
            # 1. Base timeframes
            if timeframe == TF_1S:
                return self._builder_1s.get_active_candle()
            if timeframe == TF_1M:
                return self._builder_1m.get_active_candle()

            # 2. Higher timeframes: dynamic rollup of closed 1M bars + active 1M bar
            staging = list(self._staging_1m_for_higher_tf.get(timeframe, []))
            active_1m = self._builder_1m.get_active_candle()

            if not staging and active_1m is None:
                return None

            # Determine higher timeframe bucket bounds
            ref_time = staging[0].start_time if staging else active_1m.start_time
            b_start, b_end = self.calendar.get_bucket_bounds(ref_time, timeframe, self.instrument_id)

            components: List[Candle] = []
            if staging:
                components.extend([b for b in staging if b_start <= b.start_time < b_end])
            if active_1m is not None and b_start <= active_1m.start_time < b_end:
                components.append(active_1m)

            if not components:
                return None

            open_price = components[0].open
            high_price = max(c.high for c in components)
            low_price = min(c.low for c in components)
            close_price = components[-1].close
            total_vol = sum(c.volume for c in components)
            total_ticks = sum(c.ticks for c in components)

            v_quality = VolumeQuality.COMPLETE
            for c in components:
                if c.volume_quality != VolumeQuality.COMPLETE:
                    v_quality = c.volume_quality
                    break

            open_oi = components[0].open_oi
            valid_high_ois = [c.high_oi for c in components if c.high_oi is not None]
            high_oi = max(valid_high_ois) if valid_high_ois else None
            valid_low_ois = [c.low_oi for c in components if c.low_oi is not None]
            low_oi = min(valid_low_ois) if valid_low_ois else None
            close_oi = components[-1].close_oi

            sum_pv = sum(c.vwap * c.volume for c in components if c.vwap is not None and c.volume > 0)
            vwap = (sum_pv / total_vol) if total_vol > 0 else close_price

            return Candle(
                instrument_id=self.instrument_id,
                timeframe=timeframe,
                start_time=b_start,
                end_time=b_end,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=total_vol,
                ticks=total_ticks,
                volume_quality=v_quality,
                open_oi=open_oi,
                high_oi=high_oi,
                low_oi=low_oi,
                close_oi=close_oi,
                vwap=round(vwap, 4),
                is_closed=False,
                session_id=components[0].session_id,
            )

    # =========================================================================
    # CLOCK-ASSISTED FINALIZATION & HISTORY
    # =========================================================================

    def finalize_until(self, market_time: datetime) -> List[Candle]:
        """
        Clock-assisted coordinator finalization.
        Finalizes active 1S and 1M if market_time >= active.end_time,
        cascades to higher timeframes, and seals completed buckets.
        """
        closed: List[Candle] = []
        with self._lock:
            # Finalize 1S if due
            c_1s = self._builder_1s.finalize_if_due(market_time)
            if c_1s:
                self._history[TF_1S].append(c_1s)
                closed.append(c_1s)

            # Finalize 1M if due
            c_1m = self._builder_1m.finalize_if_due(market_time)
            if c_1m:
                self._history[TF_1M].append(c_1m)
                closed.append(c_1m)
                higher_closed = self._cascade_closed_1m_bar(c_1m)
                closed.extend(higher_closed)

            # Check if any staging buckets for higher timeframes are now past market_time
            for tf in self._higher_timeframes:
                staging = self._staging_1m_for_higher_tf[tf]
                if staging:
                    b_start, b_end = self.calendar.get_bucket_bounds(staging[0].start_time, tf, self.instrument_id)
                    if market_time >= b_end:
                        finalized = self._rollup_closed_bars(staging, tf, b_start, b_end)
                        self._history[tf].append(finalized)
                        closed.append(finalized)
                        staging.clear()

        return closed

    def force_finalize_all(self) -> List[Candle]:
        """Flushes all open bars at session close or shutdown."""
        closed: List[Candle] = []
        with self._lock:
            c_1s = self._builder_1s.force_finalize()
            if c_1s:
                self._history[TF_1S].append(c_1s)
                closed.append(c_1s)

            c_1m = self._builder_1m.force_finalize()
            if c_1m:
                self._history[TF_1M].append(c_1m)
                closed.append(c_1m)
                higher_closed = self._cascade_closed_1m_bar(c_1m)
                closed.extend(higher_closed)

            for tf in self._higher_timeframes:
                staging = self._staging_1m_for_higher_tf[tf]
                if staging:
                    b_start, b_end = self.calendar.get_bucket_bounds(staging[0].start_time, tf, self.instrument_id)
                    finalized = self._rollup_closed_bars(staging, tf, b_start, b_end)
                    self._history[tf].append(finalized)
                    closed.append(finalized)
                    staging.clear()

        return closed

    def get_history(self, timeframe: TimeFrame, count: Optional[int] = None) -> List[Candle]:
        """Returns an immutable snapshot list of historical closed candles."""
        with self._lock:
            ring = self._history.get(timeframe)
            if not ring:
                return []
            if count is not None and count > 0:
                return list(ring)[-count:]
            return list(ring)

    @property
    def late_ticks_rejected(self) -> int:
        return self._builder_1s.late_ticks_rejected + self._builder_1m.late_ticks_rejected
