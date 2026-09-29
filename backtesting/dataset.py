"""
Historical Dataset Ingestion & Validation for Tradego Backtesting.

Enforces deterministic chronological ordering, schema validation, and
strict monotonicity checks on historical MarketEvent streams.
"""

from datetime import datetime, timezone
import json
from typing import Any, Dict, Iterator, List, Optional, Sequence

from services.market_gateway.models import (
    DepthLevel,
    MarketDepth,
    MarketEvent,
)


class HistoricalDataset:
    """
    Validated, chronological in-memory dataset of canonical MarketEvents.
    Guarantees monotonic timestamp progression and deterministic iteration.
    """

    def __init__(
        self,
        events: Sequence[MarketEvent],
        sort: bool = True,
        validate_monotonic: bool = True,
    ) -> None:
        if not events:
            raise ValueError("HistoricalDataset requires at least one MarketEvent.")

        # Convert to list for indexing
        event_list = list(events)

        # Validate event schema
        for i, ev in enumerate(event_list):
            if not isinstance(ev, MarketEvent):
                raise TypeError(
                    f"Event at index {i} is not a MarketEvent instance (got {type(ev)})."
                )
            if ev.exchange_timestamp is None:
                raise ValueError(f"Event at index {i} has null exchange_timestamp.")

        if sort:
            # Deterministic stable sort: primary key exchange_timestamp, secondary key sequence_number
            event_list.sort(
                key=lambda e: (
                    e.exchange_timestamp,
                    (e.raw or {}).get("sequence_number", 0) if isinstance(e.raw, dict) else 0,
                )
            )

        if validate_monotonic:
            prev_ts: Optional[datetime] = None
            for idx, ev in enumerate(event_list):
                if prev_ts is not None and ev.exchange_timestamp < prev_ts:
                    raise ValueError(
                        f"Chronological ordering violation at index {idx}: "
                        f"Timestamp {ev.exchange_timestamp.isoformat()} is earlier than "
                        f"preceding timestamp {prev_ts.isoformat()}."
                    )
                prev_ts = ev.exchange_timestamp

        self._events: List[MarketEvent] = event_list

    def __len__(self) -> int:
        return len(self._events)

    def __getitem__(self, index: int) -> MarketEvent:
        return self._events[index]

    def __iter__(self) -> Iterator[MarketEvent]:
        return iter(self._events)

    @property
    def start_time(self) -> datetime:
        return self._events[0].exchange_timestamp

    @property
    def end_time(self) -> datetime:
        return self._events[-1].exchange_timestamp

    @classmethod
    def from_events(
        cls,
        events: Sequence[MarketEvent],
        sort: bool = True,
        validate_monotonic: bool = True,
    ) -> "HistoricalDataset":
        """Factory creating dataset directly from an event sequence."""
        return cls(events=events, sort=sort, validate_monotonic=validate_monotonic)

    @classmethod
    def from_jsonl(
        cls,
        file_path: str,
        provider: str = "HISTORICAL_REPLAY",
        sort: bool = True,
    ) -> "HistoricalDataset":
        """
        Parses JSON-Lines file of tick dictionaries into canonical MarketEvents.
        Expected JSON keys: provider_symbol_id, exchange_timestamp, ltp, volume (optional depth).
        """
        events: List[MarketEvent] = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                
                # Parse timestamp
                raw_ts = data.get("exchange_timestamp")
                if not raw_ts:
                    raise ValueError(f"Line {line_no}: missing 'exchange_timestamp'.")
                if isinstance(raw_ts, str):
                    ts = datetime.fromisoformat(raw_ts)
                else:
                    ts = datetime.fromtimestamp(raw_ts, tz=timezone.utc)

                # Parse depth if present
                depth: Optional[MarketDepth] = None
                if "depth" in data and isinstance(data["depth"], dict):
                    bids = [
                        DepthLevel(price=b["price"], quantity=b["quantity"], orders=b.get("orders", 1))
                        for b in data["depth"].get("bids", [])
                    ]
                    asks = [
                        DepthLevel(price=a["price"], quantity=a["quantity"], orders=a.get("orders", 1))
                        for a in data["depth"].get("asks", [])
                    ]
                    depth = MarketDepth(bids=bids, asks=asks)

                sym_id = data.get("provider_symbol_id") or data.get("symbol")
                if not sym_id:
                    raise ValueError(f"Line {line_no}: missing symbol identification.")

                ev = MarketEvent(
                    provider=data.get("provider", provider),
                    provider_symbol_id=sym_id,
                    exchange_timestamp=ts,
                    local_receive_datetime=ts,
                    ltp=float(data.get("ltp", data.get("price", 0.0))),
                    ltp_qty=int(data.get("ltp_qty", data.get("ltq", data.get("quantity", 0)))),
                    tick_volume=float(data.get("tick_volume", data.get("volume", 0.0))),
                    total_volume=int(data.get("total_volume", data.get("volume", 0))),
                    oi=int(data.get("oi", data.get("open_interest", 0))),
                    depth=depth,
                    raw={"sequence_number": data.get("sequence_number", line_no)},
                )
                events.append(ev)

        return cls(events=events, sort=sort, validate_monotonic=True)
