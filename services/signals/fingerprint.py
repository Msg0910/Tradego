"""
Canonical Deterministic Signal Fingerprinting and Reaffirmation Hashing (Phase 5).

Implements strictly deterministic SHA-256 fingerprinting using normalized canonical JSON
serialization with sorted keys and compact separators. Zero arbitrary string concatenation.
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional

from services.market_state.instrument import InstrumentId


def serialize_instrument_id(instrument_id: InstrumentId) -> Dict[str, Any]:
    """Serializes InstrumentId to a canonical deterministic dictionary."""
    return {
        "symbol": instrument_id.symbol,
        "exchange": instrument_id.exchange.value,
        "instrument_type": instrument_id.instrument_type.value,
        "expiry": str(instrument_id.expiry) if instrument_id.expiry is not None else None,
        "strike": instrument_id.strike,
        "option_type": instrument_id.option_type.value if instrument_id.option_type is not None else None,
    }


def compute_config_hash(config_dict: Dict[str, Any]) -> str:
    """Computes SHA-256 hash of canonical sorted JSON configuration dictionary."""
    canonical_json = json.dumps(config_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_signal_fingerprint(
    strategy_id: str,
    strategy_version: str,
    config_hash: str,
    instrument_id: InstrumentId,
    signal_type: str,
    direction: int,
    trigger_mode: str,
    market_timestamp: datetime,
    availability_timestamp: datetime,
    suggested_entry_price: Optional[float],
    suggested_stop_loss: Optional[float],
    suggested_take_profit: Optional[float],
    regime: str,
    setup: str,
) -> str:
    """
    Computes deterministic SHA-256 fingerprint of the semantic signal decision payload.
    Volatile runtime instance fields (signal_id, generated_timestamp) are excluded so
    identical decision states produce mathematically identical fingerprints.
    """
    payload = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "config_hash": config_hash,
        "instrument_id": serialize_instrument_id(instrument_id),
        "signal_type": signal_type,
        "direction": direction,
        "trigger_mode": trigger_mode,
        "market_timestamp": market_timestamp.isoformat(),
        "availability_timestamp": availability_timestamp.isoformat(),
        "suggested_entry_price": round(suggested_entry_price, 4) if suggested_entry_price is not None else None,
        "suggested_stop_loss": round(suggested_stop_loss, 4) if suggested_stop_loss is not None else None,
        "suggested_take_profit": round(suggested_take_profit, 4) if suggested_take_profit is not None else None,
        "regime": regime,
        "setup": setup,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_reaffirmation_key(
    strategy_id: str,
    instrument_id: InstrumentId,
    setup: str,
    direction: int,
    setup_anchor_timestamp: datetime,
) -> str:
    """
    Computes deterministic SHA-256 reaffirmation key using canonical JSON serialization.
    Enables downstream systems to recognize in-flight setup continuity across evaluations.
    """
    payload = {
        "strategy_id": strategy_id,
        "instrument_id": serialize_instrument_id(instrument_id),
        "setup": setup,
        "direction": direction,
        "setup_anchor_timestamp": setup_anchor_timestamp.isoformat(),
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
