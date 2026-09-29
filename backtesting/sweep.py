"""
Deterministic Parameter Optimization Grid Sweep for Tradego.

Executes Cartesian parameter combinations across isolated backtest runs,
ranking outcomes by user-defined financial objectives.
"""

from copy import deepcopy
import itertools
from typing import Any, Dict, List, Optional
import uuid

from .dataset import HistoricalDataset
from .engine import BacktestEngine
from .models import BacktestConfig, SweepConfig, SweepResult, SweepRunResult


class ParameterSweepRunner:
    """
    Deterministic grid search runner for strategy optimization.
    Executes each parameter set in a completely isolated BacktestEngine instance.
    """

    def __init__(self, engine: Optional[BacktestEngine] = None) -> None:
        self._engine = engine or BacktestEngine()

    def run(self, sweep_config: SweepConfig, dataset: HistoricalDataset) -> SweepResult:
        if not sweep_config.param_grid:
            raise ValueError("param_grid cannot be empty.")

        sweep_id = f"SWEEP-{uuid.uuid4().hex[:8].upper()}"

        # Generate Cartesian product of parameters
        param_names = list(sweep_config.param_grid.keys())
        param_values = list(sweep_config.param_grid.values())
        combinations = [
            dict(zip(param_names, prod))
            for prod in itertools.product(*param_values)
        ]

        run_results: List[SweepRunResult] = []

        for idx, params in enumerate(combinations, 1):
            merged_params = deepcopy(sweep_config.base_config.strategy_params)
            merged_params.update(params)

            run_config = BacktestConfig(
                symbol=sweep_config.base_config.symbol,
                strategy_class=sweep_config.base_config.strategy_class,
                strategy_params=merged_params,
                initial_cash=sweep_config.base_config.initial_cash,
                start_time=sweep_config.base_config.start_time,
                end_time=sweep_config.base_config.end_time,
                slippage_bps=sweep_config.base_config.slippage_bps,
                lot_size=sweep_config.base_config.lot_size,
                tick_size=sweep_config.base_config.tick_size,
                max_daily_loss=sweep_config.base_config.max_daily_loss,
                run_id_prefix=f"SWP{idx:03d}",
            )

            result = self._engine.run(run_config, dataset)

            # Extract objective metric
            obj_val = float(getattr(result.metrics, sweep_config.objective_metric, 0.0))
            run_results.append(
                SweepRunResult(
                    params=params,
                    result=result,
                    objective_value=obj_val,
                )
            )

        # Sort runs by objective value descending (higher is better)
        run_results.sort(key=lambda r: r.objective_value, reverse=True)

        best_run = run_results[0]

        return SweepResult(
            sweep_id=sweep_id,
            best_params=best_run.params,
            best_result=best_run.result,
            all_runs=run_results,
        )
