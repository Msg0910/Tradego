"""
Tradego Signal Intelligence Setup Detectors (Phase 5).
"""

from .breakout import RangeBreakoutSetup
from .pullback import EMAVWAPPullbackSetup

__all__ = [
    "EMAVWAPPullbackSetup",
    "RangeBreakoutSetup",
]
