"""
Tradego API Application Boundary Package.
Exposes REST and WebSocket endpoints adhering to the approved security architecture,
deterministic sequence frontier synchronization, and structured error contract.
"""

from .app import create_app

__all__ = ["create_app"]
