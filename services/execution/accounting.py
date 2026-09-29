"""
Tradego Phase 7 Execution Layer — Position Accounting Authority.

Maintains exact transactional ledger updates to PositionSnapshot in response
to authoritative Fill events. Distinctly separates continuous fill accounting
from point-in-time PositionReconciliationAdjustment events without fabricating trades.
"""

from datetime import datetime, timezone
from typing import Optional

from services.execution.models import Fill, OrderSide, PositionReconciliationAdjustment
from services.risk.models import PositionSnapshot


class PositionAccounting:
    """
    Transactional position accounting coordinator.
    Transforms authoritative execution fills into updated immutable PositionSnapshots.
    """

    @staticmethod
    def apply_fill(
        current_snapshot: Optional[PositionSnapshot],
        fill: Fill,
        strategy_id: Optional[str] = None,
    ) -> PositionSnapshot:
        """
        Updates position state transactionally upon receipt of an authoritative Fill.
        Calculates weighted average price on increases, books realized PnL on decreases.
        """
        fill_direction = 1 if fill.side == OrderSide.BUY else -1
        fill_signed_qty = fill_direction * fill.fill_quantity

        # Case 1: Initiating a position from flat / nonexistent
        if current_snapshot is None or current_snapshot.is_flat:
            realized_pnl = current_snapshot.realized_pnl if current_snapshot is not None else 0.0
            return PositionSnapshot(
                instrument_id=fill.instrument_id,
                net_quantity=fill_signed_qty,
                average_entry_price=fill.fill_price,
                current_market_price=fill.fill_price,
                unrealized_pnl=0.0,
                realized_pnl=realized_pnl,
                opened_timestamp=fill.exchange_timestamp,
                last_updated_timestamp=fill.exchange_timestamp,
                strategy_id=strategy_id or (current_snapshot.strategy_id if current_snapshot else None),
            )

        # Case 2: Position already open
        net_qty = current_snapshot.net_quantity
        is_same_direction = (net_qty > 0 and fill_direction > 0) or (net_qty < 0 and fill_direction < 0)

        if is_same_direction:
            # Position-increasing fill (scale in): recalculate weighted average entry price
            new_net_qty = net_qty + fill_signed_qty
            total_cost = (abs(net_qty) * current_snapshot.average_entry_price) + (
                fill.fill_quantity * fill.fill_price
            )
            new_avg_entry = total_cost / abs(new_net_qty)
            new_realized_pnl = current_snapshot.realized_pnl
            new_opened_ts = current_snapshot.opened_timestamp
        else:
            # Position-reducing fill (exit / scale out): book realized PnL
            reduced_qty = min(abs(net_qty), fill.fill_quantity)
            if net_qty > 0:
                # Long position trimmed by SELL
                pnl_delta = (fill.fill_price - current_snapshot.average_entry_price) * reduced_qty
            else:
                # Short position trimmed by BUY
                pnl_delta = (current_snapshot.average_entry_price - fill.fill_price) * reduced_qty

            new_realized_pnl = current_snapshot.realized_pnl + pnl_delta
            new_net_qty = net_qty + fill_signed_qty

            if new_net_qty == 0:
                # Completely closed to flat
                new_avg_entry = 0.0
                new_opened_ts = current_snapshot.opened_timestamp
            elif (net_qty > 0 and new_net_qty < 0) or (net_qty < 0 and new_net_qty > 0):
                # Direction flipped through zero (e.g. Stop & Reverse)
                new_avg_entry = fill.fill_price
                new_opened_ts = fill.exchange_timestamp
            else:
                # Partially closed: entry price of remaining position is preserved
                new_avg_entry = current_snapshot.average_entry_price
                new_opened_ts = current_snapshot.opened_timestamp

        new_unrealized_pnl = (
            (fill.fill_price - new_avg_entry) * new_net_qty if new_net_qty != 0 else 0.0
        )

        return PositionSnapshot(
            instrument_id=fill.instrument_id,
            net_quantity=new_net_qty,
            average_entry_price=round(new_avg_entry, 4) if new_net_qty != 0 else 0.0,
            current_market_price=fill.fill_price,
            unrealized_pnl=round(new_unrealized_pnl, 4),
            realized_pnl=round(new_realized_pnl, 4),
            opened_timestamp=new_opened_ts,
            last_updated_timestamp=fill.exchange_timestamp,
            strategy_id=strategy_id or current_snapshot.strategy_id,
        )

    @staticmethod
    def apply_reconciliation(
        current_snapshot: Optional[PositionSnapshot],
        adjustment: PositionReconciliationAdjustment,
    ) -> PositionSnapshot:
        """
        Applies point-in-time quantity alignment without fabricating execution fills.
        """
        prev_realized = current_snapshot.realized_pnl if current_snapshot is not None else 0.0
        prev_entry = current_snapshot.average_entry_price if current_snapshot is not None else 0.0
        prev_opened = current_snapshot.opened_timestamp if current_snapshot is not None else adjustment.timestamp
        market_price = current_snapshot.current_market_price if current_snapshot is not None else 0.0

        unrealized = (
            (market_price - prev_entry) * adjustment.broker_quantity
            if adjustment.broker_quantity != 0 and market_price > 0.0
            else 0.0
        )

        return PositionSnapshot(
            instrument_id=adjustment.instrument_id,
            net_quantity=adjustment.broker_quantity,
            average_entry_price=prev_entry if adjustment.broker_quantity != 0 else 0.0,
            current_market_price=market_price,
            unrealized_pnl=round(unrealized, 4),
            realized_pnl=prev_realized,
            opened_timestamp=prev_opened,
            last_updated_timestamp=adjustment.timestamp,
            strategy_id=current_snapshot.strategy_id if current_snapshot else None,
        )
