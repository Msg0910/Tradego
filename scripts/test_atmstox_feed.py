#!/usr/bin/env python3
"""
Tradego STEP 0: Standalone ATMSTOX Market-Data Connectivity Test.

Tests live WebSocket streaming from ATMSTOX with:
1. Low-level Engine.IO & Socket.IO wire logging enabled.
2. Wire-level event interceptor printing every event name and raw argument tuple.
3. Multi-token subscription test supporting known NSE equity and MCX commodity tokens:
   - 466583 : GOLD MCX (Commodity)
   - 13061  : 360ONE (NSE Equity)
   - 256265 : NIFTY 50 (NSE Index)
4. Detection and verification of extended market data fields (market depth, circuits, ATP, OI, etc.).
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Configure UTF-8 encoding on Windows to prevent charmap UnicodeEncodeErrors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Check for python-socketio dependency before importing client
try:
    import socketio
except ImportError:
    print("\n" + "=" * 70)
    print("❌ ERROR: 'python-socketio' is required to connect to ATMSTOX Socket.IO feed.")
    print("To install it into your Tradego virtual environment, run:")
    print("    .\\.venv\\Scripts\\pip install \"python-socketio[client]\"")
    print("=" * 70 + "\n")
    sys.exit(1)

from services.market_gateway.atmstox.client import ATMStoxClient, normalize_tick

# Known valid tokens from the existing charting system
DEFAULT_TEST_TOKENS = ["466583", "13061", "256265"]
TOKEN_DESCRIPTIONS = {
    "466583": "GOLD MCX (Commodity)",
    "13061": "360ONE (NSE Equity)",
    "256265": "NIFTY 50 (NSE Index)",
}
DEFAULT_DURATION = 60

# Extended market data fields to probe for presence in live raw payloads
FIELDS_TO_DETECT = [
    "BidQty",
    "AskQty",
    "Bid1",
    "BidQty1",
    "Bid2",
    "BidQty2",
    "Bid3",
    "BidQty3",
    "Bid4",
    "BidQty4",
    "Ask1",
    "AskQty1",
    "Ask2",
    "AskQty2",
    "Ask3",
    "AskQty3",
    "Ask4",
    "AskQty4",
    "Total_Buy",
    "Total_Sell",
    "ATP",
    "TickVolume",
    "Previous_Open_Interest_Close",
    "UC",
    "LC",
    "HIGH52",
    "LOW52",
]


def run_test(symbols: List[str], duration_sec: int, endpoint: str, wire_logging: bool = True):
    print("\n" + "=" * 80)
    print("📡 TRADEGO STEP 0: ATMSTOX MARKET-DATA CONNECTIVITY & WIRE-LEVEL TEST")
    print("=" * 80)
    print(f"Target Endpoint   : {endpoint}")
    print(f"Transport         : websocket only")
    print(f"Wire Logger       : {'ENABLED (logger=True, engineio_logger=True)' if wire_logging else 'DISABLED'}")
    print(f"Test Duration     : {duration_sec} seconds")
    print(f"Instruments ({len(symbols)})  :")
    for s in symbols:
        desc = TOKEN_DESCRIPTIONS.get(s, "Custom Instrument")
        print(f"  • Token: {s:<8} | Desc: {desc:<25} | Subscription: giverate(\"{s}\") -> trade{s}")
    print("=" * 80 + "\n")

    total_events_received = 0
    total_valid_ticks = 0
    start_time = None
    last_tick_time = None
    consecutive_intervals = []
    detected_fields: Dict[str, Dict[str, Any]] = {
        field: {"present": False, "sample_val": None, "sample_symbol": None}
        for field in FIELDS_TO_DETECT
    }

    symbol_stats: Dict[str, Dict[str, Any]] = {
        s: {
            "events": 0,
            "valid_ticks": 0,
            "null_events": 0,
            "latest_ltp": None,
            "latest_time": None,
        }
        for s in symbols
    }

    # Instantiate client with wire logging enabled
    try:
        client = ATMStoxClient(endpoint=endpoint, logger_enabled=wire_logging)
    except Exception as e:
        print(f"❌ Failed to initialize client: {e}")
        return

    # Wire-level event interceptor: Intercepts EVERY Socket.IO event received
    # and formats it specifically matching:
    # EVENT: trade466583
    # ARGS: (null,)
    orig_trigger_event = client.sio._trigger_event

    def hooked_trigger_event(event, namespace, *args):
        nonlocal total_events_received
        total_events_received += 1

        def _format_val(v):
            if v is None:
                return "null"
            return repr(v)

        if not args:
            args_str = "()"
        elif len(args) == 1:
            args_str = f"({_format_val(args[0])},)"
        else:
            args_str = "(" + ", ".join(_format_val(a) for a in args) + ")"

        print(f"\nEVENT: {event}")
        print(f"ARGS: {args_str}\n")
        sys.stdout.flush()

        return orig_trigger_event(event, namespace, *args)

    client.sio._trigger_event = hooked_trigger_event

    # Also hook ACK and connect_error packets if received across the wire
    orig_handle_ack = client.sio._handle_ack

    def hooked_handle_ack(namespace, id, data):
        print(f"\nEVENT: ack (id={id})")
        print(f"ARGS: {data}\n")
        sys.stdout.flush()
        return orig_handle_ack(namespace, id, data)

    client.sio._handle_ack = hooked_handle_ack

    orig_handle_error = client.sio._handle_error

    def hooked_handle_error(namespace, data):
        print(f"\nEVENT: connect_error")
        print(f"ARGS: ({data},)\n")
        sys.stdout.flush()
        return orig_handle_error(namespace, data)

    client.sio._handle_error = hooked_handle_error

    # Connect to ATMSTOX Socket.IO server
    try:
        print("Connecting to ATMSTOX Socket.IO server...")
        client.connect(timeout=10)
        print("✅ Connection established successfully!")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return

    # Per-symbol tick handler builder
    def make_tick_callback(sym: str):
        def on_symbol_tick(raw_data: Any):
            nonlocal total_valid_ticks, start_time, last_tick_time
            current_time = time.time()
            local_now = datetime.now()

            if start_time is None:
                start_time = current_time

            stats = symbol_stats[sym]
            stats["events"] += 1

            if raw_data is None:
                stats["null_events"] += 1
                print(
                    f"[{local_now.strftime('%H:%M:%S.%f')[:-3]}] "
                    f"[{sym} | {TOKEN_DESCRIPTIONS.get(sym, 'Symbol')}] "
                    f"⚠️ Received NULL payload from server (no active rate)."
                )
                return

            if not isinstance(raw_data, dict):
                print(
                    f"[{local_now.strftime('%H:%M:%S.%f')[:-3]}] "
                    f"[{sym}] Non-dict payload received: {repr(raw_data)}"
                )
                return

            # Valid tick dictionary received!
            stats["valid_ticks"] += 1
            total_valid_ticks += 1

            # Calculate time between valid ticks
            interval_ms = None
            if last_tick_time is not None:
                interval_ms = (current_time - last_tick_time) * 1000.0
                consecutive_intervals.append(interval_ms)
            last_tick_time = current_time

            # Calculate overall frequency
            elapsed_total = current_time - start_time
            freq = (total_valid_ticks / elapsed_total) if elapsed_total > 0 else 0.0

            # Inspect and record detected fields
            for field in FIELDS_TO_DETECT:
                if field in raw_data and raw_data[field] is not None:
                    detected_fields[field]["present"] = True
                    if detected_fields[field]["sample_val"] is None:
                        detected_fields[field]["sample_val"] = raw_data[field]
                        detected_fields[field]["sample_symbol"] = sym

            # Print formatted raw tick for the first 5 valid ticks per symbol
            if stats["valid_ticks"] <= 5:
                print(f"\n--- [RAW TICK FOR {sym} (#{stats['valid_ticks']})] ---")
                print(json.dumps(raw_data, indent=2, default=str))

            # Normalize and extract structured values
            norm = normalize_tick(raw_data, sym)

            ltp = norm.get("ltp") if norm else None
            bid = norm.get("bid") if norm else None
            ask = norm.get("ask") if norm else None
            spread = norm.get("spread") if norm else None
            ltp_qty = norm.get("ltp_qty") if norm else None
            vol = norm.get("total_volume") if norm else None
            oi = norm.get("today_oi") if norm else None
            feed_ts = norm.get("feed_timestamp_str") if norm else "N/A"

            if ltp is not None:
                stats["latest_ltp"] = ltp
            stats["latest_time"] = feed_ts

            int_str = f"{interval_ms:8.2f} ms" if interval_ms is not None else "     First"
            spread_str = f"{spread:7.2f}" if spread is not None else "    N/A"
            bid_str = f"{bid:9.2f}" if bid is not None else "      N/A"
            ask_str = f"{ask:9.2f}" if ask is not None else "      N/A"
            ltp_str = f"{ltp:9.2f}" if ltp is not None else "      N/A"

            print(
                f"[{local_now.strftime('%H:%M:%S.%f')[:-3]}] "
                f"[{sym:>6}] Tick #{stats['valid_ticks']:3d} | "
                f"LTP: {ltp_str} | "
                f"Bid: {bid_str} | "
                f"Ask: {ask_str} | "
                f"Spread: {spread_str} | "
                f"Qty: {str(ltp_qty):>4} | "
                f"Vol: {str(vol):>8} | "
                f"OI: {str(oi):>6} | "
                f"FeedTime: {feed_ts} | "
                f"Δt: {int_str} | "
                f"Rate: {freq:5.1f} t/s"
            )

        return on_symbol_tick

    # Subscribe to all test symbols
    try:
        for sym in symbols:
            desc = TOKEN_DESCRIPTIONS.get(sym, "Instrument")
            print(f"Subscribing to {sym} ({desc}) via giverate(\"{sym}\")...")
            client.subscribe(sym, callback=make_tick_callback(sym))
        print(f"✅ All {len(symbols)} subscriptions emitted! Listening for {duration_sec}s...")
        print("-" * 120)
    except Exception as e:
        print(f"❌ Subscription failed: {e}")
        client.disconnect()
        return

    # Keep alive loop
    end_time = time.time() + duration_sec
    try:
        while time.time() < end_time:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n⚠️ User interrupted test early (Ctrl+C).")
    finally:
        print("\n" + "=" * 80)
        print("🛑 CLOSING CONNECTIONS & UNSUBSCRIBING")
        print("=" * 80)
        for sym in symbols:
            try:
                print(f"Emitting stoprate(\"{sym}\")...")
                client.unsubscribe(sym)
                print(f"✅ Unsubscribed {sym}.")
            except Exception as e:
                print(f"⚠️ Unsubscribe error for {sym}: {e}")

        try:
            client.disconnect()
            print("✅ Socket disconnected.")
        except Exception as e:
            print(f"⚠️ Disconnect error: {e}")

    # Summary Report
    print("\n" + "=" * 80)
    print("📊 TEST EXECUTION SUMMARY")
    print("=" * 80)
    total_time = (time.time() - start_time) if start_time else 0
    print(f"Total Socket.IO Events Received : {total_events_received}")
    print(f"Total Valid Tick Payloads       : {total_valid_ticks}")
    print(f"Active Streaming Time           : {total_time:.2f} seconds")

    if consecutive_intervals:
        avg_interval = sum(consecutive_intervals) / len(consecutive_intervals)
        min_interval = min(consecutive_intervals)
        max_interval = max(consecutive_intervals)
        print(f"Average Interval                : {avg_interval:.2f} ms")
        print(f"Min / Max Interval              : {min_interval:.2f} ms / {max_interval:.2f} ms")
        avg_rate = total_valid_ticks / total_time if total_time > 0 else 0
        print(f"Average Tick Rate               : {avg_rate:.2f} ticks/second")

    print("\n" + "-" * 80)
    print(f"{'Token':<8} | {'Description':<24} | {'Events':<7} | {'Valid':<6} | {'Null':<5} | {'Latest LTP':<10} | {'Feed Time'}")
    print("-" * 80)
    for s in symbols:
        st = symbol_stats[s]
        desc = TOKEN_DESCRIPTIONS.get(s, "Instrument")
        ltp_s = f"{st['latest_ltp']:.2f}" if st['latest_ltp'] is not None else "N/A"
        time_s = st['latest_time'] if st['latest_time'] else "N/A"
        print(
            f"{s:<8} | {desc:<24} | {st['events']:<7} | {st['valid_ticks']:<6} | "
            f"{st['null_events']:<5} | {ltp_s:<10} | {time_s}"
        )
    print("-" * 80)

    print("\n" + "-" * 80)
    print("DETECTION OF EXTENDED MARKET DATA FIELDS ACROSS RECEIVED PAYLOADS")
    print("-" * 80)
    print(f"{'Field Name':<30} | {'Status':<10} | {'Sample Value':<20} | {'From Symbol'}")
    print("-" * 80)
    for field, info in detected_fields.items():
        status = "PRESENT ✅" if info["present"] else "ABSENT  ❌"
        val_str = str(info["sample_val"]) if info["present"] else "-"
        sym_str = str(info["sample_symbol"]) if info["present"] else "-"
        print(f"{field:<30} | {status} | {val_str:<20} | {sym_str}")
    print("-" * 80)

    print("\n✅ STEP 0 Test Completed.\n")


def main():
    parser = argparse.ArgumentParser(
        description="Tradego STEP 0: Standalone ATMSTOX Connectivity & Wire-Level Test"
    )
    parser.add_argument(
        "--symbols",
        "--symbol",
        dest="symbols",
        default=",".join(DEFAULT_TEST_TOKENS),
        help=f"Comma-separated ATMSTOX instrument tokens (default: {','.join(DEFAULT_TEST_TOKENS)})",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION,
        help=f"Test duration in seconds (default: {DEFAULT_DURATION})",
    )
    parser.add_argument(
        "--endpoint",
        default="https://atmstox.com:20100",
        help="ATMSTOX Socket.IO URL (default: https://atmstox.com:20100)",
    )
    parser.add_argument(
        "--no-wire-logger",
        action="store_true",
        help="Disable low-level Engine.IO/Socket.IO packet debug logging",
    )

    args = parser.parse_args()

    symbol_list = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbol_list:
        symbol_list = DEFAULT_TEST_TOKENS

    run_test(
        symbols=symbol_list,
        duration_sec=args.duration,
        endpoint=args.endpoint,
        wire_logging=not args.no_wire_logger,
    )


if __name__ == "__main__":
    main()
