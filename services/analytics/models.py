"""
Tradego Feature & Quantitative Analytics Models (Phase 4).

Defines immutable data models for feature observations, feature quality flags,
and thread-safe feature snapshots with safe reference sharing.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterator, Optional

from services.market_state.instrument import InstrumentId


class FeatureQuality(str, Enum):
    """
    Quality classification for a computed feature observation.
    Ensures degraded or invalid market states are never silently presented as authoritative.
    """
    VALID = "VALID"                # Authoritative, computed from complete, uncompromised data
    DEGRADED = "DEGRADED"          # Computed from partial, reconstructed, or estimated input data
    WARMING_UP = "WARMING_UP"      # Insufficient history (periods_observed < min_periods)
    STALE = "STALE"                # Time since last observation exceeds maximum staleness horizon
    INVALID = "INVALID"            # Missing input, division-by-zero, or numerical error


@dataclass(frozen=True, slots=True)
class FeatureValue:
    """
    Immutable representation of a single quantitative feature observation.
    """
    feature_id: str
    value: Optional[float]
    quality: FeatureQuality
    observation_timestamp: datetime
    availability_timestamp: datetime
    is_confirmed: bool
    metadata: Optional[Dict[str, Any]] = None

    @property
    def is_valid(self) -> bool:
        """True if the feature value is valid and not warming up or invalid."""
        return self.quality == FeatureQuality.VALID and self.value is not None

    @property
    def is_usable(self) -> bool:
        """True if the feature is either authoritative or degraded (usable with caution)."""
        return self.quality in (FeatureQuality.VALID, FeatureQuality.DEGRADED) and self.value is not None


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """
    Immutable, point-in-time collection of all active feature values for an instrument.
    Created by safe reference sharing of immutable FeatureValue instances.
    """
    instrument_id: InstrumentId
    snapshot_timestamp: datetime
    _features: Dict[str, FeatureValue] = field(default_factory=dict)

    def get(self, feature_id: str) -> Optional[FeatureValue]:
        """Returns the FeatureValue for the given feature_id, or None if not found."""
        return self._features.get(feature_id)

    def get_value(self, feature_id: str, default: Optional[float] = None) -> Optional[float]:
        """Returns the scalar float value if present and usable, else default."""
        fv = self._features.get(feature_id)
        if fv is not None and fv.value is not None:
            return fv.value
        return default

    def is_valid(self, feature_id: str) -> bool:
        """True if feature exists and has FeatureQuality.VALID."""
        fv = self._features.get(feature_id)
        return fv is not None and fv.is_valid

    def __getitem__(self, feature_id: str) -> FeatureValue:
        return self._features[feature_id]

    def __contains__(self, feature_id: str) -> bool:
        return feature_id in self._features

    def __len__(self) -> int:
        return len(self._features)

    def __iter__(self) -> Iterator[str]:
        return iter(self._features)

    def items(self):
        return self._features.items()

    def values(self):
        return self._features.values()

    def keys(self):
        return self._features.keys()
