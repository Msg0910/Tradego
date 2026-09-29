"""
Unit tests for Tradego PortfolioRuntimeState (Phase 8).
Verifies PositionAccounting integration, cash balance tracking, mark-to-market updates,
trailing watermarks for PositionView, and AccountRiskState generation.
"""

from datetime import datetime, timezone
import unittest

from services.execution.models import Fill, OrderSide
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.runtime.portfolio import PortfolioRuntimeState


class TestRuntimePortfolioAndAccounting(unittest.TestCase):
    """Verifies portfolio view synchronization and cash accounting."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="TCS",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)
        self.portfolio = PortfolioRuntimeState(
            account_id="PAPER_ACCOUNT",
            initial_cash=1_000_000.0,
        )

    def test_fill_increases_position_and_reduces_cash(self) -> None:
        """Verifies opening a long position updates cash balance and PositionSnapshot."""
        fill = Fill(
            fill_id="FILL-001",
            client_order_id="TG-001",
            broker_order_id="BRK-001",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=10,
            fill_price=3500.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000,
        )

        pos = self.portfolio.on_fill(fill, strategy_id="TREND_STRAT")
        self.assertEqual(pos.net_quantity, 10)
        self.assertEqual(pos.average_entry_price, 3500.0)
        self.assertEqual(pos.realized_pnl, 0.0)

        # Cash reduced by 10 * 3500 = 35,000
        self.assertEqual(self.portfolio.cash_balance, 1_000_000.0 - 35_000.0)

        # PositionView for Phase 5 StrategyContext
        pv = self.portfolio.get_position_view(self.instrument_id)
        self.assertIsNotNone(pv)
        self.assertEqual(pv.net_quantity, 10)
        self.assertEqual(pv.entry_price, 3500.0)
        self.assertEqual(pv.highest_price_since_entry, 3500.0)
        self.assertEqual(pv.lowest_price_since_entry, 3500.0)

    def test_mark_price_update_and_watermarks(self) -> None:
        """Verifies mark updates adjust unrealized PnL and high/low watermarks."""
        # Open position: 10 shares @ 3500
        fill = Fill(
            fill_id="FILL-001",
            client_order_id="TG-001",
            broker_order_id="BRK-001",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=10,
            fill_price=3500.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000,
        )
        self.portfolio.on_fill(fill)

        # Price rises to 3550
        self.portfolio.update_mark_price(self.instrument_id, mark_price=3550.0, timestamp=self.now)
        pos = self.portfolio.get_position(self.instrument_id)
        self.assertIsNotNone(pos)
        self.assertEqual(pos.current_market_price, 3550.0)
        self.assertEqual(pos.unrealized_pnl, (3550.0 - 3500.0) * 10)  # +500.0

        pv = self.portfolio.get_position_view(self.instrument_id)
        self.assertEqual(pv.highest_price_since_entry, 3550.0)
        self.assertEqual(pv.lowest_price_since_entry, 3500.0)

        # Price dips to 3480
        self.portfolio.update_mark_price(self.instrument_id, mark_price=3480.0, timestamp=self.now)
        pv2 = self.portfolio.get_position_view(self.instrument_id)
        self.assertEqual(pv2.highest_price_since_entry, 3550.0)
        self.assertEqual(pv2.lowest_price_since_entry, 3480.0)

    def test_closing_position_realizes_pnl_and_restores_cash(self) -> None:
        """Verifies closing a position books realized PnL and flattens PositionView."""
        # Buy 10 @ 3500
        fill1 = Fill(
            fill_id="FILL-001",
            client_order_id="TG-001",
            broker_order_id="BRK-001",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=10,
            fill_price=3500.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000,
        )
        self.portfolio.on_fill(fill1)

        # Sell 10 @ 3600 (Profit: +1000)
        fill2 = Fill(
            fill_id="FILL-002",
            client_order_id="TG-002",
            broker_order_id="BRK-002",
            instrument_id=self.instrument_id,
            side=OrderSide.SELL,
            fill_quantity=10,
            fill_price=3600.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=2000,
        )
        pos2 = self.portfolio.on_fill(fill2)
        self.assertTrue(pos2.is_flat)
        self.assertEqual(pos2.realized_pnl, 1000.0)

        # Cash balance = 1,000,000 - 35,000 + 36,000 = 1,001,000
        self.assertEqual(self.portfolio.cash_balance, 1_001_000.0)

        # PositionView for flat instrument returns None
        self.assertIsNone(self.portfolio.get_position_view(self.instrument_id))

    def test_account_risk_state_generation(self) -> None:
        """Verifies AccountRiskState metrics match active positions and cash."""
        acc_state = self.portfolio.get_account_state(self.now)
        self.assertEqual(acc_state.total_equity, 1_000_000.0)
        self.assertEqual(acc_state.available_cash, 1_000_000.0)
        self.assertEqual(acc_state.realized_pnl_today, 0.0)
        self.assertEqual(acc_state.unrealized_pnl_current, 0.0)


if __name__ == "__main__":
    unittest.main()
