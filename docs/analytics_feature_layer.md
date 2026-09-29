# Tradego Feature & Quantitative Analytics Layer (Phase 4)

## 1. Overview

The **Tradego Feature & Quantitative Analytics Layer** (`services.analytics`) bridges normalized market data from Phases 1–3 into quantitative signals, streaming indicators, and microstructure metrics required by trading strategies and risk engines.

```
[ Phase 1: MarketDataGateway / Backtest Replay ]
                         │
                         ▼ (Normalized MarketEvent stream)
[ Phase 2: InstrumentStateStore ]
                         │
                         ├─────────────────────────────────────────┐
                         ▼                                         ▼
[ Phase 3: CandleEngine ]                    [ Live State Snapshots ]
       │                                                │
       │ (Closed Candles & Active Previews)             │ (L1-L4 Depth, Micro-Price)
       │                                                │
       └────────────────────────┬───────────────────────┘
                                │
                                ▼
         ┌─────────────────────────────────────────────┐
         │   PHASE 4: FEATURE & ANALYTICS ENGINE       │
         │                                             │
         │  ┌───────────────────────────────────────┐  │
         │  │ 1. Microstructure Features (Hot Path) │  │
         │  │    Order book imbalance, spread, flow │  │
         │  ├───────────────────────────────────────┤  │
         │  │ 2. Streaming Indicators (Warm Path)   │  │
         │  │    EMA, SMA, RSI, ATR, VWAP, MACD, BB │  │
         │  ├───────────────────────────────────────┤  │
         │  │ 3. Derived Regime Features (Cold)     │  │
         │  │    Z-scores, Realized Vol, Skew       │  │
         │  └───────────────────────────────────────┘  │
         └──────────────────────┬──────────────────────┘
                                │
                                ▼
         [ FeatureSnapshot (Immutable Reference Sharing) ]
```

---

## 2. Core Architectural Guarantees

### 2.1 The Dual-Path Indicator Contract
Every stateful indicator implements:
1. `confirmed_update(candle: Candle)`:
   - Invoked strictly when a candle bucket closes (`is_closed = True`).
   - Advances persistent indicator state (e.g. `prev_ema`, `running_sum`, `buffer`).
   - Returns a `FeatureValue` with `is_confirmed = True`.
2. `preview(active_candle: Candle)`:
   - Invoked on-demand to inspect the forming in-progress candle.
   - **Must never mutate persistent indicator state.**
   - Returns an ephemeral `FeatureValue` with `is_confirmed = False`.

### 2.2 Rolling Bollinger Bands Variance
- Uses a fixed-capacity ring buffer with sliding sums and sums of squares:
  $$\text{running\_sum} \leftarrow \text{running\_sum} - x_{\text{old}} + x_{\text{new}}$$
  $$\text{running\_sum\_sq} \leftarrow \text{running\_sum\_sq} - (x_{\text{old}})^2 + (x_{\text{new}})^2$$
- Strictly safeguards against floating-point catastrophic cancellation:
  $$\sigma^2 = \max(0.0, \, \text{raw\_variance})$$

### 2.3 Volume Quality Propagation
Phase 3's `VolumeQuality` propagates directly into `FeatureQuality`:
- `COMPLETE` ➔ `FeatureQuality.VALID`
- `RECONSTRUCTED`, `PARTIAL`, `ESTIMATED` ➔ `FeatureQuality.DEGRADED`
- `UNAVAILABLE` ➔ `FeatureQuality.INVALID`

### 2.4 Open Interest Same-Timeframe Rule
- $\Delta P_t$ and $\Delta OI_t$ must originate from the exact same timeframe stream.
- If an instrument reports no Open Interest (e.g. Cash Equity), the indicator preserves `None` semantics (`value = None`, `quality = INVALID`) rather than manufacturing fake data.

### 2.5 Timestamp Semantics & Look-Ahead Prevention
- `observation_timestamp`: Market time of the underlying data.
- `availability_timestamp`: Monotonic point-in-time when the feature became validly accessible to decision logic.
- Confirmed candle features only become available at the candle boundary ($T_{\text{end}}$).

---

## 3. Component Hierarchy

| Module | Components | Responsibility |
| :--- | :--- | :--- |
| `services.analytics.models` | `FeatureValue`, `FeatureSnapshot`, `FeatureQuality` | Immutable data representations with safe reference sharing. |
| `services.analytics.base` | `BaseIndicator`, `BaseMicrostructureFeature` | Abstract contracts enforcing dual-path and hot-path execution. |
| `services.analytics.indicators` | `StreamingEMA`, `StreamingSMA`, `StreamingRSI`, `StreamingMACD`, `StreamingATR`, `RollingBollingerBands`, `StreamingVWAP`, `VolumeZScore`, `OpenInterestQuadrant` | $O(1)$ streaming stateful indicators. |
| `services.analytics.microstructure` | `BookImbalance`, `WeightedMidPrice`, `SpreadBps`, `TradeFlowImbalance`, `TickIntensity` | Hot-path depth and tick flow analytics. |
| `services.analytics.store` | `InstrumentFeatureStore` | Per-instrument state isolation and ring buffers. |
| `services.analytics.engine` | `FeatureEngine` | Central coordinator connecting to `CandleEngine` and state listeners. |

---

## 4. Measured Performance & Verification

Tested on Windows with standard CPython runtime:

- **Hot Microstructure Update Latency**:
  - p50: **6.00 µs**
  - p95: **7.10 µs**
  - p99: **12.20 µs**
- **Warm Indicator Update Latency**:
  - p50: **11.80 µs**
  - p95: **13.60 µs**
  - p99: **44.90 µs**
- **Memory Scaling**:
  - **35.85 KB per instrument** (across 50 instruments populated with features, well below the 250 KB target).
- **Test Suite**:
  - 150 tests passing (zero failures, zero errors).
