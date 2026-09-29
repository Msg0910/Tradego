"""
Tradego Portfolio Runtime State (Phase 8).

Coordinates authoritative position views and account equity snapshots.
Bridges Phase 7 PositionAccounting with Phase 6 RiskContext and Phase 5 StrategyContext.
Strictly acts as an in-memory view layer without duplicating domain calculations.
"""

from datetime import datetime, timezone
import threading
from typing import Dict, Optional

from services.execution.accounting import PositionAccounting
from services.execution.models import Fill, OrderSide
from services.market_state.instrument import InstrumentId
from services.risk.context import AccountRiskState
from services.risk.models import PortfolioSnapshot, PositionSnapshot
from services.signals.models import PositionView


class PortfolioRuntimeState:
    """
    Authoritative point-in-time view of portfolio positions and account state.
    Exposes immutable snapshots for risk and strategy evaluations.
    """

    def __init__(
        self,
        account_id: str = "PAPER_ACCOUNT",
        initial_cash: float = 1_000_000.0,
    ) -> None:
        self._lock = threading.RLock()
        self._account_id = account_id
        self._cash_balance = initial_cash
        self._peak_equity = initial_cash
        self._positions: Dict[InstrumentId, PositionSnapshot] = {}
        self._high_watermarks: Dict[InstrumentId, float] = {}
        self._low_watermarks: Dict[InstrumentId, float] = {}

    @property
    def cash_balance(self) -> float:
        with self._lock:
            return self._cash_balance

    def on_fill(self, fill: Fill, strategy_id: Optional[str] = None) -> PositionSnapshot:
        """
        Processes an authoritative fill event.
        Delegates exact transactional position math to Phase 7 PositionAccounting.
        Updates cash balance and trailing watermark references.
        """
        with self._lock:
            prev_snapshot = self._positions.get(fill.instrument_id)
            new_snapshot = PositionAccounting.apply_fill(
                current_snapshot=prev_snapshot,
                fill=fill,
                strategy_id=strategy_id,
            )
            self._positions[fill.instrument_id] = new_snapshot

            # Update cash balance
            fill_notional = fill.fill_quantity * fill.fill_price
            if fill.side == OrderSide.BUY:
                self._cash_balance -= fill_notional
            else:
                self._cash_balance += fill_notional

            # Update trailing high/low watermarks for StrategyContext PositionView
            if prev_snapshot is None or prev_snapshot.is_flat:
                self._high_watermarks[fill.instrument_id] = fill.fill_price
                self._low_watermarks[fill.instrument_id] = fill.fill_price
            else:
                current_high = self._high_watermarks.get(fill.instrument_id, fill.fill_price)
                current_low = self._low_watermarks.get(fill.instrument_id, fill.fill_price)
                self._high_watermarks[fill.instrument_id] = max(current_high, fill.fill_price)
                self._low_watermarks[fill.instrument_id] = min(current_low, fill.fill_price)

            # Update peak equity
            total_equity = self._calculate_total_equity_internal()
            if total_equity > self._peak_equity:
                self._peak_equity = total_equity

            return new_snapshot

    def update_mark_price(
        self,
        instrument_id: InstrumentId,
        mark_price: float,
        timestamp: datetime,
    ) -> None:
        """
        Updates the current market price for an open position and recalculates unrealized PnL.
        """
        with self._lock:
            snap = self._positions.get(instrument_id)
            if snap is not None and not snap.is_flat:
                unrealized = (mark_price - snap.average_entry_price) * snap.net_quantity
                updated_snap = PositionSnapshot(
                    instrument_id=snap.instrument_id,
                    net_quantity=snap.net_quantity,
                    average_entry_price=snap.average_entry_price,
                    current_market_price=mark_price,
                    unrealized_pnl=unrealized,
                    realized_pnl=snap.realized_pnl,
                    opened_timestamp=snap.opened_timestamp,
                    last_updated_timestamp=timestamp,
                    strategy_id=snap.strategy_id,
                )
                self._positions[instrument_id] = updated_snap

                # Update watermarks
                current_high = self._high_watermarks.get(instrument_id, mark_price)
                current_low = self._low_watermarks.get(instrument_id, mark_price)
                self._high_watermarks[instrument_id] = max(current_high, mark_price)
                self._low_watermarks[instrument_id] = min(current_low, mark_price)

                total_equity = self._calculate_total_equity_internal()
                if total_equity > self._peak_equity:
                    self._peak_equity = total_equity

    def get_position(self, instrument_id: InstrumentId) -> Optional[PositionSnapshot]:
        """Returns the current PositionSnapshot for an instrument."""
        with self._lock:
            return self._positions.get(instrument_id)

    def get_portfolio_snapshot(self, timestamp: Optional[datetime] = None) -> PortfolioSnapshot:
        """
        Returns an immutable PortfolioSnapshot for Phase 6 RiskContext.
        """
        now = timestamp or datetime.now(timezone.utc)
        with self._lock:
            # Create shallow copy of dict so mapping proxy is thread-safe
            pos_copy = dict(self._positions)
            return PortfolioSnapshot(
                snapshot_timestamp=now,
                positions=pos_copy,
            )

    def get_position_view(self, instrument_id: InstrumentId) -> Optional[PositionView]:
        """
        Returns an immutable PositionView for Phase 5 StrategyContext.
        Returns None if the instrument has no open position or is flat.
        """
        with self._lock:
            snap = self._positions.get(instrument_id)
            if snap is None or snap.is_flat:
                return None

            high = self._high_watermarks.get(instrument_id, snap.average_entry_price)
            low = self._low_watermarks.get(instrument_id, snap.average_entry_price)

            return PositionView(
                instrument_id=instrument_id,
                net_quantity=snap.net_quantity,
                entry_price=snap.average_entry_price,
                entry_time=snap.opened_timestamp,
                highest_price_since_entry=high,
                lowest_price_since_entry=low,
                unrealized_pnl_estimate=snap.unrealized_pnl,
            )

    def get_account_state(self, timestamp: Optional[datetime] = None) -> AccountRiskState:
        """
        Returns an immutable AccountRiskState for Phase 6 RiskContext.
        """
        now = timestamp or datetime.now(timezone.utc)
        with self._lock:
            total_unrealized = sum(p.unrealized_pnl for p in self._positions.values() if not p.is_flat)
            total_realized = sum(p.realized_pnl for p in self._positions.values())
            total_equity = self._cash_balance + total_unrealized

            return AccountRiskState(
                account_id=self._account_id,
                total_equity=total_equity,
                available_cash=self._cash_balance,
                peak_equity=self._peak_equity,
                realized_pnl_today=total_realized,
                unrealized_pnl_current=total_unrealized,
                last_updated_timestamp=now,
            )

    def _calculate_total_equity_internal(self) -> float:
        total_unrealized = sum(p.unrealized_pnl for p in self._positions.values() if not p.is_flat)
        return self._cash_balance + total_unrealized
