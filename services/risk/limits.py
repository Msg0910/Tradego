"""
Tradego Risk Limits and Configuration Versioning (Phase 6).

Defines immutable RiskLimits with deterministic SHA-256 configuration hashing.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional


def compute_risk_config_hash(config_dict: Dict[str, Any]) -> str:
    """Computes deterministic SHA-256 hash of canonical sorted JSON configuration dictionary."""
    clean_dict = dict(config_dict)
    clean_dict.pop("config_hash", None)
    canonical_json = json.dumps(clean_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """
    Immutable, versioned risk parameter set.
    Traceable across all risk decisions via config_version and config_hash.
    """
    config_version: str = "1.0.0"
    config_hash: str = ""
    max_risk_per_trade_pct: float = 0.01                 # 1% max account-level risk per trade
    max_risk_per_trade_absolute: Optional[float] = None  # Optional absolute monetary cap
    max_capital_allocation_per_trade_pct: float = 0.20   # 20% max capital allocation per trade
    max_gross_leverage: float = 2.0                      # Max total gross exposure / equity
    max_net_leverage: float = 1.0                        # Max total net exposure / equity
    max_concurrent_positions: int = 10                   # Max open positions
    max_daily_loss_pct: float = 0.03                     # 3% max daily loss limit
    max_drawdown_pct: float = 0.10                       # 10% peak-to-trough limit
    max_instrument_exposure_pct: float = 0.25            # 25% max exposure per instrument
    min_risk_reward_ratio: float = 1.5                   # Required geometry R:R (entry only)
    min_stop_distance_bps: float = 10.0                  # 10 bps minimum stop distance
    max_stop_distance_bps: float = 500.0                 # 500 bps (5%) maximum stop distance
    max_signal_age_seconds: float = 60.0                 # Max age for BAR_CLOSE signals
    allow_degraded_features: bool = False                # Strictly reject DEGRADED by default
    strategy_budgets_pct: Optional[Mapping[str, float]] = None # Strategy-specific risk budget fraction

    def __post_init__(self) -> None:
        if self.strategy_budgets_pct is not None and isinstance(self.strategy_budgets_pct, dict):
            object.__setattr__(
                self, "strategy_budgets_pct", MappingProxyType(self.strategy_budgets_pct)
            )

        if not self.config_hash:
            cfg_dict = {
                "config_version": self.config_version,
                "max_risk_per_trade_pct": self.max_risk_per_trade_pct,
                "max_risk_per_trade_absolute": self.max_risk_per_trade_absolute,
                "max_capital_allocation_per_trade_pct": self.max_capital_allocation_per_trade_pct,
                "max_gross_leverage": self.max_gross_leverage,
                "max_net_leverage": self.max_net_leverage,
                "max_concurrent_positions": self.max_concurrent_positions,
                "max_daily_loss_pct": self.max_daily_loss_pct,
                "max_drawdown_pct": self.max_drawdown_pct,
                "max_instrument_exposure_pct": self.max_instrument_exposure_pct,
                "min_risk_reward_ratio": self.min_risk_reward_ratio,
                "min_stop_distance_bps": self.min_stop_distance_bps,
                "max_stop_distance_bps": self.max_stop_distance_bps,
                "max_signal_age_seconds": self.max_signal_age_seconds,
                "allow_degraded_features": self.allow_degraded_features,
                "strategy_budgets_pct": dict(self.strategy_budgets_pct)
                if self.strategy_budgets_pct is not None
                else None,
            }
            h = compute_risk_config_hash(cfg_dict)
            object.__setattr__(self, "config_hash", h)

