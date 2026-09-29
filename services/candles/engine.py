"""
Tradego Central Candle Engine: Ingestion, Lifecycle, and Multi-Instrument Coordinator.

Connects to MarketDataGateway via listener pattern.
Routes incoming MarketEvents to per-instrument InstrumentCandleSeries.
Manages closed candle dispatching to registered callbacks with exception isolation.
Provides session finalization hooks (finalize_until, force_finalize_all).
"""

import threading
from datetime import datetime
from typing import Callable, Dict, List, Optional

from services.market_gateway.models import MarketEvent
from services.market_state.instrument import InstrumentId, InstrumentRegistry
from .calendar import ExchangeCalendar, IndianMarketCalendar
from .models import Candle
from .series import InstrumentCandleSeries
from .timeframe import TimeFrame
from .volume import CumulativeVolumePolicy, VolumeAccountingPolicy


class CandleEngine:
    """
    Central coordinator managing real-time candle aggregation across all instruments.
    """

    def __init__(
        self,
        registry: InstrumentRegistry,
        calendar: Optional[ExchangeCalendar] = None,
        volume_policy_factory: Optional[Callable[[InstrumentId], VolumeAccountingPolicy]] = None,
        capacities: Optional[Dict[TimeFrame, int]] = None,
    ) -> None:
        self._registry = registry
        self._calendar = calendar or IndianMarketCalendar()
        self._volume_policy_factory = (
            volume_policy_factory or (lambda iid: CumulativeVolumePolicy())
        )
        self._capacities = capacities

        # Per-instrument series storage protected by engine lock for addition
        self._series_lock = threading.Lock()
        self._series: Dict[InstrumentId, InstrumentCandleSeries] = {}

        # Downstream closed candle listeners
        self._listener_lock = threading.Lock()
        self._listeners: List[Callable[[Candle], None]] = []

        # Diagnostic metrics
        self._unresolved_ticks_count: int = 0
        self._listener_error_count: int = 0

    # =========================================================================
    # INGESTION HOT PATH
    # =========================================================================

    def on_market_event(self, event: MarketEvent) -> List[Candle]:
        """
        Hot path ingestion callback registered with MarketDataGateway.
        Resolves the canonical InstrumentId, routes the tick to the per-instrument series,
        and dispatches any finalized candles to downstream listeners.
        """
        iid = self._registry.resolve(event.provider, event.provider_symbol_id)
        if iid is None:
            # Token is unregistered; never guess or fallback silently to NSE
            self._unresolved_ticks_count += 1
            return []

        series = self._get_or_create_series(iid)
        closed_candles = series.process_tick(event)

        if closed_candles:
            self._dispatch_closed_candles(closed_candles)

        return closed_candles

    def _get_or_create_series(
        self,
        instrument_id: InstrumentId,
        volume_policy: Optional[VolumeAccountingPolicy] = None,
    ) -> InstrumentCandleSeries:
        """
        Retrieves or initializes the InstrumentCandleSeries for the given InstrumentId.
        Thread-safe double-checked lookup.
        """
        # Fast path without lock
        series = self._series.get(instrument_id)
        if series is not None:
            return series

        # Slow path under lock
        with self._series_lock:
            series = self._series.get(instrument_id)
            if series is None:
                policy = volume_policy or self._volume_policy_factory(instrument_id)
                series = InstrumentCandleSeries(
                    instrument_id=instrument_id,
                    calendar=self._calendar,
                    volume_policy=policy,
                    capacities=self._capacities,
                )
                self._series[instrument_id] = series
            return series

    def _dispatch_closed_candles(self, closed_candles: List[Candle]) -> None:
        """Dispatches finalized candles to all registered listeners with error isolation."""
        with self._listener_lock:
            listeners = list(self._listeners)

        if not listeners:
            return

        for candle in closed_candles:
            for listener in listeners:
                try:
                    listener(candle)
                except Exception:
                    self._listener_error_count += 1

    # =========================================================================
    # REGISTRATION & LISTENER MANAGEMENT
    # =========================================================================

    def add_candle_listener(self, callback: Callable[[Candle], None]) -> None:
        """Registers a callback to receive immutable finalized Candle instances."""
        with self._listener_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_candle_listener(self, callback: Callable[[Candle], None]) -> None:
        """Unregisters a previously added candle callback."""
        with self._listener_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    # Alias for convenience
    add_listener = add_candle_listener
    remove_listener = remove_candle_listener

    # =========================================================================
    # QUERY API
    # =========================================================================

    def get_series(self, instrument_id: InstrumentId) -> Optional[InstrumentCandleSeries]:
        """Returns the series for an instrument if initialized."""
        return self._series.get(instrument_id)

    def get_active_candle(
        self, instrument_id: InstrumentId, timeframe: TimeFrame
    ) -> Optional[Candle]:
        """
        Returns the real-time active (forming) candle preview for the instrument.
        Uses Path B on-demand rollup with zero lock contention on tick ingestion.
        """
        series = self._series.get(instrument_id)
        if series is None:
            return None
        return series.get_active_candle(timeframe)

    def get_history(
        self,
        instrument_id: InstrumentId,
        timeframe: TimeFrame,
        count: Optional[int] = None,
    ) -> List[Candle]:
        """
        Returns an immutable snapshot list of historical closed candles for the timeframe.
        """
        series = self._series.get(instrument_id)
        if series is None:
            return []
        return series.get_history(timeframe, count)

    # =========================================================================
    # SESSION FINALIZATION HOOKS
    # =========================================================================

    def finalize_until(self, market_time: datetime) -> List[Candle]:
        """
        Clock-assisted coordinator finalization.
        Iterates across all managed series and finalizes any active candles whose
        end_time <= market_time.
        Used at session boundaries, market breaks, or for illiquid instruments.
        """
        with self._series_lock:
            all_series = list(self._series.values())

        total_closed: List[Candle] = []
        for series in all_series:
            closed = series.finalize_until(market_time)
            if closed:
                total_closed.extend(closed)

        if total_closed:
            self._dispatch_closed_candles(total_closed)

        return total_closed

    def force_finalize_all(self) -> List[Candle]:
        """
        Flushes all open active bars across all managed series.
        Used at session close or process shutdown.
        """
        with self._series_lock:
            all_series = list(self._series.values())

        total_closed: List[Candle] = []
        for series in all_series:
            closed = series.force_finalize_all()
            if closed:
                total_closed.extend(closed)

        if total_closed:
            self._dispatch_closed_candles(total_closed)

        return total_closed

    # =========================================================================
    # METRICS & DIAGNOSTICS
    # =========================================================================

    @property
    def active_instruments_count(self) -> int:
        return len(self._series)

    @property
    def unresolved_ticks_count(self) -> int:
        return self._unresolved_ticks_count

    @property
    def late_ticks_rejected(self) -> int:
        with self._series_lock:
            return sum(s.late_ticks_rejected for s in self._series.values())

    @property
    def listener_error_count(self) -> int:
        return self._listener_error_count
