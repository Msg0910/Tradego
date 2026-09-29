"""
Report and Artifact Generation for Tradego Backtesting Subsystem.

Formats BacktestResult and SweepResult objects into structured Markdown,
serializable JSON summaries, and equity curve time-series.
"""

from datetime import datetime
import json
from typing import Any, Dict, List

from .models import BacktestResult, SweepResult


class ReportGenerator:
    """Formats backtest and sweep outcomes for human inspection and API export."""

    @staticmethod
    def to_markdown(result: BacktestResult, max_trades_to_show: int = 15) -> str:
        """Generates comprehensive Markdown performance report."""
        cfg = result.config
        m = result.metrics

        strat_name = cfg.strategy_class.__name__
        start_str = result.start_time.isoformat() if result.start_time else "N/A"
        end_str = result.end_time.isoformat() if result.end_time else "N/A"

        md = [
            f"# TradeGo Backtest Performance Report — {result.run_id}",
            "",
            f"**Strategy:** `{strat_name}` | **Symbol:** `{cfg.symbol}`",
            f"**Period:** `{start_str}` to `{end_str}`",
            f"**Execution Duration:** {result.execution_duration_seconds:.2f}s",
            "",
            "## 1. Executive Performance Summary",
            "",
            "| Metric | Value | Metric | Value |",
            "| :--- | :--- | :--- | :--- |",
            f"| **Initial Cash** | ₹{m.initial_cash:,.2f} | **Final Equity** | ₹{m.final_equity:,.2f} |",
            f"| **Total Return** | {m.total_return_pct:+.2f}% | **CAGR** | {m.cagr_pct:+.2f}% |",
            f"| **Max Drawdown** | {m.max_drawdown_pct:.2f}% | **Max DD Duration** | {m.max_drawdown_duration_seconds:.0f}s |",
            f"| **Sharpe Ratio** | {m.sharpe_ratio:.4f} | **Sortino Ratio** | {m.sortino_ratio:.4f} |",
            f"| **Calmar Ratio** | {m.calmar_ratio:.4f} | **Profit Factor** | {m.profit_factor:.2f} |",
            f"| **Win Rate** | {m.win_rate:.1f}% | **Payoff Ratio** | {m.payoff_ratio:.2f} |",
            f"| **Total Trades** | {m.total_trades} | **Winning / Losing** | {m.winning_trades} / {m.losing_trades} |",
            f"| **Realized PnL** | ₹{m.total_realized_pnl:+,.2f} | **Total Fills** | {len(result.fills)} |",
            "",
            "## 2. Strategy Parameters",
            "",
            "```json",
            json.dumps(cfg.strategy_params, indent=2),
            "```",
            "",
            f"## 3. Trade Log (Showing up to {max_trades_to_show} trades)",
            "",
        ]

        if not result.trades:
            md.append("*No completed trades during this simulation run.*")
        else:
            md.extend([
                "| Trade ID | Side | Qty | Entry Price | Exit Price | PnL (₹) | Return % | Duration (s) |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ])
            for t in result.trades[:max_trades_to_show]:
                side_str = t.side.value if hasattr(t.side, "value") else str(t.side)
                md.append(
                    f"| `{t.trade_id}` | {side_str} | {t.quantity} | "
                    f"{t.entry_price:.2f} | {t.exit_price:.2f} | "
                    f"{t.pnl:+,.2f} | {t.return_pct:+.2f}% | {t.holding_duration_seconds:.0f} |"
                )
            if len(result.trades) > max_trades_to_show:
                md.append(f"\n*... and {len(result.trades) - max_trades_to_show} more trades.*")

        md.append("")
        return "\n".join(md)

    @staticmethod
    def to_json(result: BacktestResult, indent: int = 2) -> str:
        """Serializes BacktestResult into structured JSON."""
        data = {
            "run_id": result.run_id,
            "symbol": result.config.symbol,
            "strategy": result.config.strategy_class.__name__,
            "strategy_params": result.config.strategy_params,
            "start_time": result.start_time.isoformat() if result.start_time else None,
            "end_time": result.end_time.isoformat() if result.end_time else None,
            "execution_duration_seconds": result.execution_duration_seconds,
            "metrics": {
                "initial_cash": result.metrics.initial_cash,
                "final_equity": result.metrics.final_equity,
                "total_return_pct": result.metrics.total_return_pct,
                "cagr_pct": result.metrics.cagr_pct,
                "max_drawdown_pct": result.metrics.max_drawdown_pct,
                "max_drawdown_duration_seconds": result.metrics.max_drawdown_duration_seconds,
                "sharpe_ratio": result.metrics.sharpe_ratio,
                "sortino_ratio": result.metrics.sortino_ratio,
                "calmar_ratio": result.metrics.calmar_ratio,
                "win_rate": result.metrics.win_rate,
                "profit_factor": result.metrics.profit_factor,
                "payoff_ratio": result.metrics.payoff_ratio,
                "total_trades": result.metrics.total_trades,
                "winning_trades": result.metrics.winning_trades,
                "losing_trades": result.metrics.losing_trades,
                "total_realized_pnl": result.metrics.total_realized_pnl,
            },
            "equity_curve": [
                {
                    "timestamp": pt.timestamp.isoformat(),
                    "total_equity": pt.total_equity,
                    "cash": pt.cash,
                    "drawdown_pct": pt.drawdown_pct,
                }
                for pt in result.equity_curve
            ],
            "trades": [
                {
                    "trade_id": t.trade_id,
                    "side": t.side.value if hasattr(t.side, "value") else str(t.side),
                    "quantity": t.quantity,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat(),
                    "pnl": t.pnl,
                    "return_pct": t.return_pct,
                    "holding_duration_seconds": t.holding_duration_seconds,
                }
                for t in result.trades
            ],
        }
        return json.dumps(data, indent=indent)

    @staticmethod
    def sweep_to_markdown(sweep_result: SweepResult) -> str:
        """Formats ParameterSweepRunner output into ranked comparative Markdown table."""
        md = [
            f"# TradeGo Parameter Sweep Report — {sweep_result.sweep_id}",
            "",
            f"**Total Grid Iterations:** {len(sweep_result.all_runs)}",
            "",
            "## 1. Optimal Parameter Set",
            "",
            "```json",
            json.dumps(sweep_result.best_params, indent=2),
            "```",
            "",
            f"**Best Objective Value:** {sweep_result.all_runs[0].objective_value:.4f}",
            f"**Best Total Return:** {sweep_result.best_result.metrics.total_return_pct:+.2f}%",
            f"**Best Sharpe Ratio:** {sweep_result.best_result.metrics.sharpe_ratio:.4f}",
            f"**Best Max Drawdown:** {sweep_result.best_result.metrics.max_drawdown_pct:.2f}%",
            "",
            "## 2. Ranked Grid Evaluation Results",
            "",
            "| Rank | Parameters | Objective | Return % | Sharpe | Max DD % | Trades | Win Rate % |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]

        for rank, run in enumerate(sweep_result.all_runs, 1):
            m = run.result.metrics
            param_str = ", ".join(f"{k}={v}" for k, v in run.params.items())
            md.append(
                f"| #{rank} | `{param_str}` | {run.objective_value:.4f} | "
                f"{m.total_return_pct:+.2f}% | {m.sharpe_ratio:.4f} | "
                f"{m.max_drawdown_pct:.2f}% | {m.total_trades} | {m.win_rate:.1f}% |"
            )

        md.append("")
        return "\n".join(md)
