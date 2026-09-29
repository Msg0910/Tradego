# Tradego Market Data Gateway Architecture (Phase 1)

## 1. Overview

The **Tradego Market Data Gateway** is an asynchronous, high-throughput, provider-agnostic market data ingestion layer. It decouples the Tradego trading system from specific market data feeds (starting with ATMSTOX) and brokers.

```
                       [ Market Data Feed (ATMSTOX) ]
                                      │ (Socket.IO / WS)
                                      ▼
                       [ ATMStoxClient (Transport) ]
                                      │ (Raw tick + monotonic timestamp)
                                      ▼
                       [ ATMStoxAdapter ]  ◄── BaseMarketDataProvider
                                      │
                                      ▼
                        [ MarketDataGateway ]
                     ┌────────────────┴────────────────┐
                     ▼                                 ▼
           [ ATMStoxNormalizer ]             [ FeedHealthMetrics ]
                     │                                 │
                     ▼                                 │
              [ MarketEvent ]                          │
                     │                                 │
                     ▼                                 │
             [ Event Dispatch ] ───────────────────────┘
                     │ (Synchronous in-memory callbacks)
                     ▼
          [ Downstream State Engine ] (Phase 2)
```

---

## 2. Critical Performance Constraints

To achieve consistent low-latency execution and avoid dropping socket buffer frames, the critical tick processing path adheres strictly to the following rules:

1. **Pipeline Path**:
   `network tick` ➔ `parse` ➔ `normalize` ➔ `publish internal event` ➔ `market state`
2. **Zero Blocking Operations**:
   Registered listeners and internal processors **MUST NOT** perform:
   - Database queries or writes
   - Kafka, Redis, or message broker network round-trips
   - HTTP requests or external API calls
   - LLM calls or MCP tool invocations
   - Synchronous file logging / disk writes
   - `print()` statements in production loops
3. **Allocation-Conscious**:
   Data structures use `__slots__` where appropriate, lightweight metric counters, and avoid object thrashing.

---

## 3. Canonical Data Model (`MarketEvent`)

Each tick is converted into a normalized `MarketEvent` dataclass:

### Key Semantic Fields & Invariants
- **`tick_volume: Optional[float]`**: Preserves fractional tick volume emitted by feeds (e.g. `8026.99`).
- **`previous_open_interest_close: Optional[float]`**: Preserves the exact semantic meaning of `Previous_Open_Interest_Close` without assuming it represents an integer quantity.
- **Market Depth**: Preserves up to 4 levels for bids and asks (`DepthLevel(price, quantity, orders)`).
- **`raw: Optional[Dict[str, Any]]`**: Preserves the complete, unmodified original provider payload for audit and lossless access.

### Timestamp Semantics
Tradego strictly separates provider/exchange clock domains from local system time:
- **`exchange_timestamp`**: Parsed exchange timestamp (e.g. from `LastExchangeUpdateTime`).
- **`provider_timestamp`**: Parsed provider broadcast timestamp (e.g. from `Timestamp` or `LastUpdateTime`).
- **`raw_last_update_time`**: Original provider string/value preserved as-is.
- **`raw_last_exchange_update_time`**: Original provider string/value preserved as-is.
- **`raw_timestamp`**: Original provider string/value preserved as-is.
- **`local_receive_timestamp`**: Monotonic `time.perf_counter()` recorded at immediate network arrival.
- **`local_receive_datetime`**: UTC datetime recorded at local receive time.
- **`normalized_timestamp`**: Monotonic `time.perf_counter()` recorded when normalization finishes.

### Latency Semantics
- **`latency_provider_to_receive_ms`**: Delta between provider broadcast timestamp and local receive datetime.
  *(Note: This reflects provider-to-receive network latency, NOT true exchange-to-system latency).*
- **`latency_gateway_process_ms`**: High-resolution processing duration in milliseconds from local receive to normalization completion.

---

## 4. Subscription & Reconnection Lifecycle

1. **Subscription Management**:
   - `gateway.subscribe(symbol_id)` registers the symbol with the underlying provider.
   - Subscriptions are deduplicated; duplicate calls are idempotent.
   - `gateway.unsubscribe(symbol_id)` gracefully stops the stream.
2. **Reconnection & Resilience**:
   - When the underlying transport reconnects, all active subscriptions in `subscribed_symbols` are automatically re-emitted to the feed server (`giverate`).
   - Subscription state is preserved in memory across connection drops.

---

## 5. Feed Health Metrics

`FeedHealthMetrics` maintains lightweight in-memory counters per symbol and across the feed:
- `ticks_received`: Total network payloads received.
- `ticks_normalized`: Valid ticks successfully converted to `MarketEvent`.
- `ticks_dropped`: Malformed or null rate payloads.
- `errors_count`: Listener or parsing errors.
- `min / max / avg gateway processing latency (ms)`.

---

## 6. Testing

- **Unit Tests**: Test normalization, missing fields, numeric conversions, null handling, subscription state, duplicate subscriptions, unsubscribe, reconnect mock, and latency calculations.
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests/unit -p "test_*.py" -v
  ```
- **Integration Tests (Opt-in)**:
  Connects to ATMSTOX live feed (`https://atmstox.com:20100`) without placing orders or hardcoding credentials.
  ```powershell
  $env:TRADEGO_LIVE_TEST="1"
  .\.venv\Scripts\python.exe -m unittest tests/integration/test_atmstox_live.py -v
  ```
