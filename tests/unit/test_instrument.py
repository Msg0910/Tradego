"""
Unit tests for Instrument Identity and Registry Abstraction.
"""

import unittest
from datetime import date

from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentMetadata,
    InstrumentRegistry,
    InstrumentType,
    OptionType,
)


class TestInstrumentIdentity(unittest.TestCase):

    def test_valid_equity_creation(self):
        eq = InstrumentId(
            symbol="360ONE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.assertEqual(eq.symbol, "360ONE")
        self.assertEqual(eq.exchange, Exchange.NSE)
        self.assertEqual(eq.instrument_type, InstrumentType.EQUITY)
        self.assertEqual(eq.canonical_id, "NSE:360ONE")

    def test_valid_index_creation(self):
        idx = InstrumentId(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.INDEX,
        )
        self.assertEqual(idx.canonical_id, "NSE:NIFTY")

    def test_valid_futures_creation(self):
        exp = date(2026, 10, 5)
        fut = InstrumentId(
            symbol="GOLD",
            exchange=Exchange.MCX,
            instrument_type=InstrumentType.FUTURES,
            expiry=exp,
        )
        self.assertEqual(fut.canonical_id, "MCX:GOLD_20261005_FUT")

    def test_valid_options_creation(self):
        exp = date(2026, 9, 24)
        opt = InstrumentId(
            symbol="NIFTY",
            exchange=Exchange.NFO,
            instrument_type=InstrumentType.OPTIONS,
            expiry=exp,
            strike=24000.0,
            option_type=OptionType.CE,
        )
        self.assertEqual(opt.canonical_id, "NFO:NIFTY_20260924_24000_CE")

    def test_equity_validation_rules(self):
        # Equity cannot have expiry
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="RELIANCE",
                exchange=Exchange.NSE,
                instrument_type=InstrumentType.EQUITY,
                expiry=date(2026, 12, 31),
            )

        # Equity cannot have strike
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="RELIANCE",
                exchange=Exchange.NSE,
                instrument_type=InstrumentType.EQUITY,
                strike=2500.0,
            )

        # Equity cannot have option_type
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="RELIANCE",
                exchange=Exchange.NSE,
                instrument_type=InstrumentType.EQUITY,
                option_type=OptionType.CE,
            )

        # Equity must be on NSE or BSE
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="RELIANCE",
                exchange=Exchange.MCX,
                instrument_type=InstrumentType.EQUITY,
            )

    def test_index_validation_rules(self):
        # Index cannot have strike
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="NIFTY",
                exchange=Exchange.NSE,
                instrument_type=InstrumentType.INDEX,
                strike=24000.0,
            )

    def test_futures_validation_rules(self):
        # Futures MUST have expiry
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="GOLD",
                exchange=Exchange.MCX,
                instrument_type=InstrumentType.FUTURES,
                expiry=None,
            )

        # Futures cannot have strike
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="GOLD",
                exchange=Exchange.MCX,
                instrument_type=InstrumentType.FUTURES,
                expiry=date(2026, 10, 5),
                strike=75000.0,
            )

    def test_options_validation_rules(self):
        exp = date(2026, 9, 24)
        # Options MUST have expiry
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="NIFTY",
                exchange=Exchange.NFO,
                instrument_type=InstrumentType.OPTIONS,
                expiry=None,
                strike=24000.0,
                option_type=OptionType.CE,
            )

        # Options MUST have positive strike
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="NIFTY",
                exchange=Exchange.NFO,
                instrument_type=InstrumentType.OPTIONS,
                expiry=exp,
                strike=0.0,
                option_type=OptionType.CE,
            )

        # Options MUST have option_type CE or PE (cannot be NONE)
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="NIFTY",
                exchange=Exchange.NFO,
                instrument_type=InstrumentType.OPTIONS,
                expiry=exp,
                strike=24000.0,
                option_type=OptionType.NONE,
            )

    def test_empty_symbol_rejected(self):
        with self.assertRaises(ValueError):
            InstrumentId(
                symbol="  ",
                exchange=Exchange.NSE,
                instrument_type=InstrumentType.EQUITY,
            )

    def test_hashable_and_comparable(self):
        eq1 = InstrumentId(symbol="360ONE", exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY)
        eq2 = InstrumentId(symbol="360one", exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY)
        self.assertEqual(eq1, eq2)
        self.assertEqual(hash(eq1), hash(eq2))
        d = {eq1: "present"}
        self.assertEqual(d[eq2], "present")


class TestInstrumentRegistry(unittest.TestCase):

    def setUp(self):
        self.registry = InstrumentRegistry()
        self.inst_360one = InstrumentId(
            symbol="360ONE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.inst_gold = InstrumentId(
            symbol="GOLD",
            exchange=Exchange.MCX,
            instrument_type=InstrumentType.FUTURES,
            expiry=date(2026, 10, 5),
        )

    def test_register_and_resolve(self):
        self.registry.register(
            instrument_id=self.inst_360one,
            provider_tokens={"ATMSTOX": "13061", "DHAN": "13061"},
            lot_size=1,
            tick_size=0.05,
        )

        resolved = self.registry.resolve("ATMSTOX", "13061")
        self.assertEqual(resolved, self.inst_360one)

        resolved_dhan = self.registry.resolve("DHAN", "13061")
        self.assertEqual(resolved_dhan, self.inst_360one)

        meta = self.registry.get_metadata(self.inst_360one)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.lot_size, 1)

    def test_never_silent_fallback_to_nse(self):
        """
        CRITICAL MANDATORY REQUIREMENT:
        Never silently fallback an unresolved provider token to Exchange.NSE.
        Unknown instruments must remain explicitly unresolved.
        """
        unknown_result = self.registry.resolve("ATMSTOX", "999999")
        self.assertIsNone(unknown_result)
        self.assertIn(("ATMSTOX", "999999"), self.registry.unresolved_tokens)

    def test_lookup_by_canonical_id(self):
        self.registry.register(self.inst_gold, {"ATMSTOX": "466583"})
        found = self.registry.get_by_canonical_id("MCX:GOLD_20261005_FUT")
        self.assertEqual(found, self.inst_gold)


if __name__ == "__main__":
    unittest.main()
