# Tradego Real-Time Market State Layer (Phase 2)

## 1. Overview

The **Tradego Market State Layer** translates normalized streams of `MarketEvent` instances from Phase 1 into a coherent, consistent, in-memory representation of market state.

```
[ Phase 1: MarketDataGateway ]
              │
              ▼ (Normalized MarketEvent stream)
[ Phase 2: InstrumentStateStore ]
       ┌──────┴────────────────────────┐
       ▼                               ▼
[ InstrumentState ]            [ OrderBookState ]
  (LTP, OHLC, Vol, OI)          (4-Level Depth, Spread, Mid)
       │
       ▼ (Thread-safe read-only snapshot)
[ InstrumentStateSnapshot ]
       │
       ├───────────────────────────────┐
       ▼                               ▼
[ Future Analytics / Candles ]   [ Future Strategy Engine ]
```

---

## 2. Core Components

### 2.1 Instrument Identity Abstraction (`InstrumentId`, `InstrumentRegistry`)
- Decouples Tradego's internal logic from external vendor/broker tokens.
- **Validation Rules**:
  - `EQUITY`: Exchange must be `NSE` or `BSE`. `expiry`, `strike`, and `option_type` must be `None`.
  - `INDEX`: `expiry`, `strike`, and `option_type` must be `None`.
  - `FUTURES`: `expiry` is mandatory. `strike` and `option_type` must be `None`.
  - `OPTIONS`: `expiry` and positive `strike` are mandatory. `option_type` must be `CE` or `PE`.
- **CRITICAL INVARIANT**: Unresolved provider tokens are **NEVER silently mapped to Exchange.NSE**. Unmapped tokens are explicitly flagged as unresolved (`is_resolved = False`, `instrument_id = None`) and tracked in `unresolved_tokens`.

### 2.2 Order Book State (`OrderBookState`)
- Represents up to 4 levels of market depth for bids and asks using immutable tuples.
- **Analytics Properties (Computed on Demand)**:
  - `best_bid`, `best_ask`, `spread` (`best_ask - best_bid`)
  - `mid_price` (`(best_bid + best_ask) / 2.0`)
  - `weighted_mid_price` (volume-weighted top level mid price)
  - `book_imbalance` (`(total_buy_qty - total_sell_qty) / (total_buy_qty + total_sell_qty)`)

### 2.3 Instrument State (`InstrumentState`)
- Maintains mutable live session state:
  - `ltp`, `ltp_qty`, `prev_ltp`, `tick_direction` (+1 uptick, -1 downtick, 0 unchanged)
  - `open`, `high`, `low`, `previous_close`, `change`, `change_percent`
  - `tick_volume` (supports fractional values), `total_volume`, `atp`
  - `oi`, `prev_oi`, `oi_change`, `previous_open_interest_close`
  - `upper_circuit`, `lower_circuit`, `high_52`, `low_52`, `digit`
- **Memory Optimization**: Does NOT retain the full `MarketEvent` to avoid unnecessary object retention on the hot path.

### 2.4 Explicitly Distinguished Timestamps
1. **`last_local_receive_timestamp`**: Monotonic `perf_counter` recorded at network arrival; defines local processing/arrival ordering.
2. **`last_exchange_timestamp`**: Exchange clock timestamp; defines market-time ordering.
3. **`last_provider_timestamp`**: Feed provider broadcast timestamp; used for provider diagnostics and network latency estimates (NOT true exchange latency).

### 2.5 State Store (`InstrumentStateStore`)
- In-memory repository with O(1) lookups by `InstrumentId` or `(provider, token)`.
- **Concurrency**: Fine-grained per-instrument locks prevent contention across different symbols.
- **Snapshots**: Produces frozen `InstrumentStateSnapshot` instances for strategy/analytics consumers without holding locks during downstream processing.
- **Session Reset**: `reset_session()` hook resets OHLC/volume boundaries for new trading sessions without hardcoding calendar hours.

---

## 3. State Update Semantics

1. **Non-Destructive Merge**: Valid state is never overwritten by `None` or omitted fields.
2. **Intra-Second Activity**: Indian market feeds broadcast timestamps with 1-second resolution (`DD-MM-YYYY HH:MM:SS`). Multiple ticks within the same second are recognized as valid market bursts.
3. **Out-of-Order Rejection**: Ticks with provider timestamps strictly older (≥ 1s) than current state increment `out_of_order_count` and are prevented from altering session high/low or LTP.

---

## 4. Candle Engine (Design Specification Only)

Candle generation is designed as an independent downstream consumer:
- **TimeFrames**: `TICK`, `1S`, `1M`, `3M`, `5M`, `15M`, `30M`, `1H`, `1D`.
- **Alignment**: Fixed wall-clock epoch buckets aligned to Indian Standard Time (`Asia/Kolkata`).
- **Decoupled Flow**: Aggregators subscribe to `InstrumentStateStore` snapshots or `MarketDataGateway` events without introducing latency into the single-tick hot path.
