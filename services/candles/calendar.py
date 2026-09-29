"""
Exchange Calendar and Trading Session Abstraction for Tradego.

Hierarchical Structure:
Exchange -> MarketSegment -> TradingSession -> SessionSegment

Decouples exchange-specific trading hours, auction phases, breaks, and holidays
from aggregation and builder logic. Supports session-anchored bucket alignment.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from .timeframe import TimeFrame, TimeFrameType

# Default Market Timezone for Indian Exchanges (robust fallback if tzdata not installed on Windows)
try:
    INDIA_TZ = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone as dt_timezone
    INDIA_TZ = dt_timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")

try:
    UTC_TZ = ZoneInfo("UTC")
except Exception:
    from datetime import timezone as dt_timezone
    UTC_TZ = dt_timezone.utc


class MarketSegment(str, Enum):
    """Trading segment within an exchange."""
    EQUITY = "EQUITY"
    DERIVATIVE = "DERIVATIVE"
    COMMODITY = "COMMODITY"
    CURRENCY = "CURRENCY"
    INDEX = "INDEX"


class SessionSegmentType(str, Enum):
    """Operational phase within a daily trading session."""
    PRE_OPEN = "PRE_OPEN"          # Call auction / order collection
    PRE_OPEN_BUFFER = "BUFFER"     # Auction matching / buffer window
    REGULAR = "REGULAR"            # Continuous normal trading
    BREAK = "BREAK"                # Mid-session break (e.g. MCX agri break)
    POST_CLOSE = "POST_CLOSE"      # Post-market closing price calculation
    CLOSED = "CLOSED"              # Market closed


@dataclass(frozen=True, slots=True)
class SessionSegment:
    """A distinct time window within a trading session."""
    segment_type: SessionSegmentType
    start_time: time
    end_time: time
    is_trading_active: bool = True  # True if continuous trade ticks occur


@dataclass(frozen=True, slots=True)
class TradingSession:
    """
    Complete trading schedule for a specific exchange, segment, and calendar date.
    """
    session_id: str
    session_date: date
    exchange: Exchange
    market_segment: MarketSegment
    timezone: ZoneInfo
    segments: Tuple[SessionSegment, ...]
    is_holiday: bool = False

    def get_regular_start(self) -> datetime:
        """Returns the start datetime of continuous regular trading."""
        for seg in self.segments:
            if seg.segment_type == SessionSegmentType.REGULAR:
                return datetime.combine(self.session_date, seg.start_time, tzinfo=self.timezone)
        # Fallback to first segment if no regular segment
        first_time = self.segments[0].start_time if self.segments else time(9, 15)
        return datetime.combine(self.session_date, first_time, tzinfo=self.timezone)

    def get_regular_end(self) -> datetime:
        """Returns the end datetime of continuous regular trading."""
        for seg in self.segments:
            if seg.segment_type == SessionSegmentType.REGULAR:
                return datetime.combine(self.session_date, seg.end_time, tzinfo=self.timezone)
        last_time = self.segments[-1].end_time if self.segments else time(15, 30)
        return datetime.combine(self.session_date, last_time, tzinfo=self.timezone)

    def get_segment(self, market_dt: datetime) -> SessionSegment:
        """Returns the active SessionSegment for a given market datetime."""
        t = market_dt.time()
        for seg in self.segments:
            if seg.start_time <= t < seg.end_time:
                return seg
        return SessionSegment(
            segment_type=SessionSegmentType.CLOSED,
            start_time=t,
            end_time=t,
            is_trading_active=False,
        )

    def is_regular_trading(self, market_dt: datetime) -> bool:
        """True if the market datetime falls within the continuous regular trading segment."""
        if self.is_holiday:
            return False
        seg = self.get_segment(market_dt)
        return seg.segment_type == SessionSegmentType.REGULAR


class ExchangeCalendar(ABC):
    """
    Abstract contract for exchange calendars and trading schedules.
    """

    @abstractmethod
    def get_session(
        self, exchange: Exchange, segment: MarketSegment, session_date: date
    ) -> TradingSession:
        """Returns the TradingSession schedule for the exchange, segment, and date."""
        pass

    @abstractmethod
    def is_trading_day(self, exchange: Exchange, session_date: date) -> bool:
        """True if the date is an active trading day (not a weekend or exchange holiday)."""
        pass

    def resolve_session(self, instrument_id: InstrumentId, market_dt: datetime) -> TradingSession:
        """Resolves the active trading session for an instrument at a specific market datetime."""
        seg_mapping = {
            InstrumentType.EQUITY: MarketSegment.EQUITY,
            InstrumentType.INDEX: MarketSegment.INDEX,
            InstrumentType.FUTURES: MarketSegment.DERIVATIVE,
            InstrumentType.OPTIONS: MarketSegment.DERIVATIVE,
            InstrumentType.COMMODITY: MarketSegment.COMMODITY,
            InstrumentType.CURRENCY: MarketSegment.CURRENCY,
        }
        segment = seg_mapping.get(instrument_id.instrument_type, MarketSegment.EQUITY)
        return self.get_session(instrument_id.exchange, segment, market_dt.date())

    def get_bucket_bounds(
        self,
        market_time: datetime,
        timeframe: TimeFrame,
        instrument_id: InstrumentId,
    ) -> Tuple[datetime, datetime]:
        """
        Calculates session-anchored [bucket_start, bucket_end) for an instrument and timeframe.
        Ensures non-hour boundaries (e.g. 09:15) align cleanly with multi-minute bars.
        """
        session = self.resolve_session(instrument_id, market_time)
        session_start = session.get_regular_start()
        session_end = session.get_regular_end()

        # Daily timeframe: spans full regular session
        if timeframe.tf_type == TimeFrameType.DAY:
            return session_start, session_end

        tf_sec = timeframe.seconds
        if tf_sec == 0:  # TICK
            return market_time, market_time

        offset_sec = int((market_time - session_start).total_seconds())
        if offset_sec < 0:
            bucket_idx = -((-offset_sec + tf_sec - 1) // tf_sec)
        else:
            bucket_idx = offset_sec // tf_sec

        bucket_start = session_start + timedelta(seconds=bucket_idx * tf_sec)
        bucket_end = bucket_start + timedelta(seconds=tf_sec)
        return bucket_start, bucket_end


class IndianMarketCalendar(ExchangeCalendar):
    """
    Authoritative calendar for Indian financial exchanges:
    - NSE / BSE Equity: Pre-Open (09:00-09:08), Buffer (09:08-09:15), Regular (09:15-15:30), Post-Close (15:40-16:00)
    - NFO Derivatives: Regular (09:15-15:30, no pre-open)
    - MCX Commodities: Regular (09:00-23:30)
    - CDS Currency: Regular (09:00-17:00)
    """

    def __init__(self, holidays: Optional[Dict[Exchange, Set[date]]] = None) -> None:
        self.holidays = holidays or {}

    def is_trading_day(self, exchange: Exchange, session_date: date) -> bool:
        # Weekends
        if session_date.weekday() >= 5:
            return False
        # Holidays
        if session_date in self.holidays.get(exchange, set()):
            return False
        return True

    def get_session(
        self, exchange: Exchange, segment: MarketSegment, session_date: date
    ) -> TradingSession:
        is_holiday = not self.is_trading_day(exchange, session_date)
        session_id = f"{exchange.value}_{segment.value}_{session_date.strftime('%Y%m%d')}"

        if exchange in (Exchange.NSE, Exchange.BSE) and segment == MarketSegment.EQUITY:
            segments = (
                SessionSegment(SessionSegmentType.PRE_OPEN, time(9, 0), time(9, 8), is_trading_active=False),
                SessionSegment(SessionSegmentType.PRE_OPEN_BUFFER, time(9, 8), time(9, 15), is_trading_active=False),
                SessionSegment(SessionSegmentType.REGULAR, time(9, 15), time(15, 30), is_trading_active=True),
                SessionSegment(SessionSegmentType.POST_CLOSE, time(15, 40), time(16, 0), is_trading_active=False),
            )
        elif exchange == Exchange.MCX or segment == MarketSegment.COMMODITY:
            segments = (
                SessionSegment(SessionSegmentType.REGULAR, time(9, 0), time(23, 30), is_trading_active=True),
            )
        elif exchange == Exchange.CDS or segment == MarketSegment.CURRENCY:
            segments = (
                SessionSegment(SessionSegmentType.REGULAR, time(9, 0), time(17, 0), is_trading_active=True),
            )
        else:
            # Default NFO / Derivatives / Index: 09:15 to 15:30
            segments = (
                SessionSegment(SessionSegmentType.REGULAR, time(9, 15), time(15, 30), is_trading_active=True),
            )

        return TradingSession(
            session_id=session_id,
            session_date=session_date,
            exchange=exchange,
            market_segment=segment,
            timezone=INDIA_TZ,
            segments=segments,
            is_holiday=is_holiday,
        )


class DefaultContinuousCalendar(ExchangeCalendar):
    """
    Continuous 24x7 calendar without breaks or holidays.
    Useful for testing, synthetic instruments, or crypto markets.
    """

    def __init__(self, tz: Optional[object] = None) -> None:
        self.tz = tz or UTC_TZ

    def is_trading_day(self, exchange: Exchange, session_date: date) -> bool:
        return True

    def get_session(
        self, exchange: Exchange, segment: MarketSegment, session_date: date
    ) -> TradingSession:
        return TradingSession(
            session_id=f"{exchange.value}_{session_date.strftime('%Y%m%d')}",
            session_date=session_date,
            exchange=exchange,
            market_segment=segment,
            timezone=self.tz,
            segments=(
                SessionSegment(SessionSegmentType.REGULAR, time(0, 0), time(23, 59, 59), is_trading_active=True),
            ),
            is_holiday=False,
        )
