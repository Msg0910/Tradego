"""
Statistical & Financial Performance Metrics for Tradego Backtesting.

Computes exact risk-adjusted and trade distribution metrics:
- Total Return, CAGR
- Maximum Drawdown, Drawdown Duration
- Sharpe Ratio, Sortino Ratio, Calmar Ratio
- Win Rate, Profit Factor, Payoff Ratio
"""

from datetime import datetime, timedelta
import math
from typing import List, Optional

from .models import EquityPoint, PerformanceMetricsResult, TradeRecord


class PerformanceMetrics:
    """Pure mathematical performance evaluator."""

    @staticmethod
    def calculate(
        initial_cash: float,
        final_equity: float,
        equity_curve: List[EquityPoint],
        trades: List[TradeRecord],
        risk_free_rate_pct: float = 0.0,
    ) -> PerformanceMetricsResult:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be strictly positive.")

        total_return_pct = ((final_equity - initial_cash) / initial_cash) * 100.0

        # Time span calculation
        start_ts: Optional[datetime] = equity_curve[0].timestamp if equity_curve else None
        end_ts: Optional[datetime] = equity_curve[-1].timestamp if equity_curve else None
        total_seconds = (end_ts - start_ts).total_seconds() if (start_ts and end_ts) else 0.0
        days = total_seconds / 86400.0

        # CAGR calculation
        if days >= 30.0 and initial_cash > 0 and final_equity > 0:
            years = days / 365.25
            cagr_pct = ((final_equity / initial_cash) ** (1.0 / years) - 1.0) * 100.0
        else:
            cagr_pct = total_return_pct

        # Drawdown calculation
        max_drawdown_pct = 0.0
        max_drawdown_duration_seconds = 0.0
        peak = initial_cash
        peak_ts: Optional[datetime] = start_ts
        current_dd_start_ts: Optional[datetime] = None

        for pt in equity_curve:
            if pt.total_equity > peak:
                peak = pt.total_equity
                peak_ts = pt.timestamp
                current_dd_start_ts = None
            else:
                dd_pct = ((peak - pt.total_equity) / peak) * 100.0
                if dd_pct > max_drawdown_pct:
                    max_drawdown_pct = dd_pct
                if current_dd_start_ts is None:
                    current_dd_start_ts = pt.timestamp
                current_duration = (pt.timestamp - current_dd_start_ts).total_seconds()
                if current_duration > max_drawdown_duration_seconds:
                    max_drawdown_duration_seconds = current_duration

        # Periodic returns for Sharpe and Sortino
        returns: List[float] = []
        for i in range(1, len(equity_curve)):
            prev_eq = equity_curve[i - 1].total_equity
            curr_eq = equity_curve[i].total_equity
            if prev_eq > 0:
                returns.append((curr_eq - prev_eq) / prev_eq)

        annualization_factor = math.sqrt(252)  # Standard daily annualization assumption

        # Sharpe Ratio
        if len(returns) > 1:
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
            std_ret = math.sqrt(variance)
            rf_periodic = (risk_free_rate_pct / 100.0) / 252.0
            sharpe_ratio = ((mean_ret - rf_periodic) / std_ret * annualization_factor) if std_ret > 1e-12 else 0.0
        else:
            sharpe_ratio = 0.0

        # Sortino Ratio
        if len(returns) > 1:
            downside_returns = [min(0.0, r) for r in returns]
            downside_variance = sum(r ** 2 for r in downside_returns) / (len(returns) - 1)
            downside_std = math.sqrt(downside_variance)
            rf_periodic = (risk_free_rate_pct / 100.0) / 252.0
            sortino_ratio = ((mean_ret - rf_periodic) / downside_std * annualization_factor) if downside_std > 1e-12 else 0.0
        else:
            sortino_ratio = 0.0

        # Calmar Ratio
        if max_drawdown_pct > 1e-6:
            calmar_ratio = cagr_pct / max_drawdown_pct
        else:
            calmar_ratio = 0.0

        # Trade statistics
        total_trades = len(trades)
        winning_trades = len([t for t in trades if t.pnl > 0])
        losing_trades = len([t for t in trades if t.pnl < 0])
        win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

        gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))

        if gross_loss > 1e-6:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 1e-6:
            profit_factor = 999.99  # Cap when zero loss
        else:
            profit_factor = 0.0

        avg_win = (gross_profit / winning_trades) if winning_trades > 0 else 0.0
        avg_loss = (gross_loss / losing_trades) if losing_trades > 0 else 0.0
        payoff_ratio = (avg_win / avg_loss) if avg_loss > 1e-6 else 0.0

        total_realized_pnl = sum(t.pnl for t in trades)

        return PerformanceMetricsResult(
            total_return_pct=round(total_return_pct, 4),
            cagr_pct=round(cagr_pct, 4),
            max_drawdown_pct=round(max_drawdown_pct, 4),
            max_drawdown_duration_seconds=round(max_drawdown_duration_seconds, 2),
            sharpe_ratio=round(sharpe_ratio, 4),
            sortino_ratio=round(sortino_ratio, 4),
            calmar_ratio=round(calmar_ratio, 4),
            win_rate=round(win_rate, 2),
            profit_factor=round(profit_factor, 4),
            payoff_ratio=round(payoff_ratio, 4),
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            initial_cash=initial_cash,
            final_equity=round(final_equity, 2),
            total_realized_pnl=round(total_realized_pnl, 2),
        )
