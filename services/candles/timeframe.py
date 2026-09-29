"""
TimeFrame Abstraction for Tradego Candle & Time-Series Aggregation.

Provides a strongly-typed, immutable representation for aggregation intervals,
supporting fixed-duration seconds, standard sub-daily intervals, and session daily bars.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar


class TimeFrameType(str, Enum):
    """Supported fundamental timeframe units."""
    TICK = "TICK"
    SECOND = "SECOND"
    MINUTE = "MINUTE"
    HOUR = "HOUR"
    DAY = "DAY"


@dataclass(frozen=True, slots=True)
class TimeFrame:
    """
    Immutable representation of an aggregation interval.
    Hashable and safe for use as dictionary keys.
    """
    tf_type: TimeFrameType
    multiplier: int = 1

    def __post_init__(self) -> None:
        if self.tf_type == TimeFrameType.TICK:
            if self.multiplier != 1:
                object.__setattr__(self, "multiplier", 1)
        else:
            if self.multiplier <= 0:
                raise ValueError(f"TimeFrame multiplier must be a positive integer, got {self.multiplier}")

    @property
    def seconds(self) -> int:
        """Fixed duration in seconds (0 for TICK)."""
        mapping = {
            TimeFrameType.TICK: 0,
            TimeFrameType.SECOND: self.multiplier,
            TimeFrameType.MINUTE: self.multiplier * 60,
            TimeFrameType.HOUR: self.multiplier * 3600,
            TimeFrameType.DAY: self.multiplier * 86400,
        }
        return mapping[self.tf_type]

    @property
    def label(self) -> str:
        """Human-readable string representation (e.g. '1S', '5M', '1H', '1D', 'TICK')."""
        if self.tf_type == TimeFrameType.TICK:
            return "TICK"
        prefix = {
            TimeFrameType.SECOND: "S",
            TimeFrameType.MINUTE: "M",
            TimeFrameType.HOUR: "H",
            TimeFrameType.DAY: "D",
        }[self.tf_type]
        return f"{self.multiplier}{prefix}"

    def __str__(self) -> str:
        return self.label

    @classmethod
    def from_string(cls, text: str) -> "TimeFrame":
        """
        Parses strings such as 'TICK', '1S', '1M', '3M', '5M', '15M', '30M', '1H', '1D'.
        """
        cleaned = text.strip().upper()
        if cleaned in ("TICK", "T"):
            return cls(TimeFrameType.TICK, 1)

        match = re.match(r"^(\d+)?([SMHD])$", cleaned)
        if not match:
            raise ValueError(f"Invalid TimeFrame string format: '{text}'. Expected e.g. '1S', '5M', '1H', '1D', 'TICK'.")

        mult_str, unit_char = match.groups()
        mult = int(mult_str) if mult_str else 1

        unit_mapping = {
            "S": TimeFrameType.SECOND,
            "M": TimeFrameType.MINUTE,
            "H": TimeFrameType.HOUR,
            "D": TimeFrameType.DAY,
        }
        return cls(unit_mapping[unit_char], mult)


# Standard TimeFrame Constants
TF_TICK = TimeFrame(TimeFrameType.TICK, 1)
TF_1S = TimeFrame(TimeFrameType.SECOND, 1)
TF_1M = TimeFrame(TimeFrameType.MINUTE, 1)
TF_3M = TimeFrame(TimeFrameType.MINUTE, 3)
TF_5M = TimeFrame(TimeFrameType.MINUTE, 5)
TF_15M = TimeFrame(TimeFrameType.MINUTE, 15)
TF_30M = TimeFrame(TimeFrameType.MINUTE, 30)
TF_1H = TimeFrame(TimeFrameType.HOUR, 1)
TF_1D = TimeFrame(TimeFrameType.DAY, 1)
