# TradeGo

Autonomous Algorithmic Trading Platform and Production Gateway.

## Architecture

- **Phase 9/10:** Forensic remediation, state persistence, disaster recovery, and multi-venue routing.
- **Phase 11:** Multi-timeframe composite regime classification, systematic execution models, and backtesting validation.
- **Phase 12 / Step 1 & 2:** Angel One SmartAPI live market data adapter & binary normalizer (Little-Endian SmartStream Modes 1, 2, 3) with offline simulation harness and session management.

## Environment & Requirements

- Python >= 3.12
- FastAPI, Starlette, Pydantic, Uvicorn, Argon2-cffi, websockets, websocket-client

## Running Tests

```powershell
py -3.12 -m pytest tests -q
```
