"""
Tradego Boundary Read-Only Adapters.
Provides isolated, non-mutating query adapters to expose authoritative
engine and market state to the presentation projection without bypassing
frozen Phase 1–8 core boundaries.
"""

from typing import Any, Dict, Optional

from services.market_state.store import InstrumentStateStore
from services.runtime.guards import TradingGuard


class MarketStateAdapter:
    """
    Read-only adapter consuming authoritative market state from InstrumentStateStore.
    Protects the trading core by presenting immutable snapshot dictionaries to the gateway.
    Never fabricates mock or unverified market data.
    """

    def __init__(self, state_store: Optional[InstrumentStateStore] = None) -> None:
        self._store = state_store

    @property
    def has_store(self) -> bool:
        return self._store is not None

    def get_market_snapshots(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns serializable dictionary of all active resolved instrument snapshots.
        """
        if not self._store:
            return {}

        snapshots = self._store.get_all_snapshots()
        result: Dict[str, Dict[str, Any]] = {}

        for inst_id, snap in snapshots.items():
            sym = inst_id.symbol if hasattr(inst_id, "symbol") else str(inst_id)
            result[sym] = {
                "symbol": sym,
                "provider": snap.provider,
                "provider_symbol_id": snap.provider_symbol_id,
                "is_resolved": snap.is_resolved,
                "ltp": snap.ltp,
                "ltp_qty": snap.ltp_qty,
                "change": snap.change,
                "change_percent": snap.change_percent,
                "open": snap.open,
                "high": snap.high,
                "low": snap.low,
                "total_volume": snap.total_volume,
                "oi": snap.oi,
                "tick_direction": snap.tick_direction,
            }

        return result

    def get_instrument_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Returns single instrument snapshot if resolved and tracked."""
        all_snaps = self.get_market_snapshots()
        return all_snaps.get(symbol.strip().upper())


class PortfolioRiskAdapter:
    """
    Read-only adapter consuming authoritative portfolio and risk state from
    PortfolioRuntimeState and RiskLimits.
    Protects the trading core by presenting immutable snapshot dictionaries.
    Never fabricates mock or unverified portfolio/risk data.
    """

    def __init__(
        self,
        portfolio_state: Optional[Any] = None,
        risk_limits: Optional[Any] = None,
        trading_guard: Optional[TradingGuard] = None,
    ) -> None:
        self._portfolio_state = portfolio_state
        self._risk_limits = risk_limits
        self._guard = trading_guard

    @property
    def has_portfolio(self) -> bool:
        return self._portfolio_state is not None

    @property
    def has_risk_limits(self) -> bool:
        return self._risk_limits is not None

    def get_portfolio_summary(self) -> Dict[str, Any]:
        """
        Returns authoritative portfolio summary. If unmounted, explicitly
        indicates unavailable rather than fabricating data.
        """
        if not self._portfolio_state:
            return {
                "is_available": False,
                "account_id": None,
                "cash": None,
                "available_cash": None,
                "total_equity": None,
                "peak_equity": None,
                "realized_pnl": None,
                "realized_pnl_today": None,
                "unrealized_pnl": None,
                "unrealized_pnl_current": None,
                "total_pnl_today": None,
                "drawdown_pct": None,
                "currency": None,
                "open_positions_count": 0,
                "total_gross_exposure": None,
                "total_net_exposure": None,
                "positions": [],
                "note": "Authoritative portfolio runtime state is not mounted.",
            }

        act = self._portfolio_state.get_account_state()
        snap = self._portfolio_state.get_portfolio_snapshot()
        positions_list = []
        for inst_id, p in snap.positions.items():
            sym = inst_id.symbol if hasattr(inst_id, "symbol") else str(inst_id)
            positions_list.append({
                "symbol": sym,
                "quantity": p.net_quantity,
                "net_quantity": p.net_quantity,
                "average_price": p.average_entry_price,
                "average_entry_price": p.average_entry_price,
                "current_market_price": p.current_market_price,
                "unrealized_pnl": p.unrealized_pnl,
                "realized_pnl": p.realized_pnl,
                "exposure": p.market_value,
                "market_value": p.market_value,
                "directional_exposure": p.directional_exposure,
                "strategy_id": p.strategy_id,
                "is_flat": p.is_flat,
            })

        return {
            "is_available": True,
            "account_id": act.account_id,
            "cash": act.available_cash,
            "available_cash": act.available_cash,
            "total_equity": act.total_equity,
            "peak_equity": act.peak_equity,
            "realized_pnl": act.realized_pnl_today,
            "realized_pnl_today": act.realized_pnl_today,
            "unrealized_pnl": act.unrealized_pnl_current,
            "unrealized_pnl_current": act.unrealized_pnl_current,
            "total_pnl_today": act.total_pnl_today,
            "drawdown_pct": act.drawdown_pct,
            "currency": act.currency,
            "open_positions_count": snap.open_positions_count,
            "total_gross_exposure": snap.total_gross_exposure,
            "total_net_exposure": snap.total_net_exposure,
            "positions": positions_list,
        }

    def get_risk_summary(self, guard_state: Optional[str] = None) -> Dict[str, Any]:
        """
        Returns authoritative risk limits and evaluation parameters.
        """
        g_state = guard_state or (self._guard.state.value if self._guard else "UNKNOWN")
        can_trade = self._guard.can_submit_speculative_entry() if self._guard else False
        can_exit = self._guard.can_submit_exit() if self._guard else True

        if not self._risk_limits:
            return {
                "is_available": False,
                "guard_state": g_state,
                "risk_limits_status": "UNAVAILABLE",
                "can_submit_speculative_entry": can_trade,
                "can_submit_exit": can_exit,
                "max_drawdown": None,
                "max_capital": None,
                "current_exposure": None,
                "margin_available": None,
                "max_gross_leverage": None,
                "max_net_leverage": None,
                "limits": None,
                "note": "Authoritative risk limits not mounted.",
            }

        exp = None
        if self._portfolio_state:
            snap = self._portfolio_state.get_portfolio_snapshot()
            exp = snap.total_gross_exposure

        return {
            "is_available": True,
            "guard_state": g_state,
            "risk_limits_status": "ENFORCED",
            "can_submit_speculative_entry": can_trade,
            "can_submit_exit": can_exit,
            "max_drawdown": self._risk_limits.max_drawdown_pct,
            "max_capital": self._risk_limits.max_capital_allocation_per_trade_pct,
            "current_exposure": exp,
            "margin_available": None,
            "max_gross_leverage": self._risk_limits.max_gross_leverage,
            "max_net_leverage": self._risk_limits.max_net_leverage,
            "limits": {
                "max_drawdown_pct": self._risk_limits.max_drawdown_pct,
                "max_daily_loss_pct": self._risk_limits.max_daily_loss_pct,
                "max_gross_leverage": self._risk_limits.max_gross_leverage,
                "max_net_leverage": self._risk_limits.max_net_leverage,
                "max_concurrent_positions": self._risk_limits.max_concurrent_positions,
                "max_instrument_exposure_pct": self._risk_limits.max_instrument_exposure_pct,
            },
        }

