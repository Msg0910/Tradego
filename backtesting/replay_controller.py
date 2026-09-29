"""
Historical Replay Controller for Tradego.

Provides deterministic step, seek, play, pause, and reset controls
feeding historical events directly into PipelineCoordinator.
"""

from datetime import datetime
from typing import Optional

from services.market_gateway.models import MarketEvent
from services.runtime.coordinator import PipelineCoordinator

from .clock import SimulationClock
from .dataset import HistoricalDataset
from .models import ReplayState


class ReplayController:
    """
    Deterministic replay controller governing event emission from HistoricalDataset
    into PipelineCoordinator.
    """

    def __init__(
        self,
        dataset: HistoricalDataset,
        coordinator: PipelineCoordinator,
        clock: Optional[SimulationClock] = None,
    ) -> None:
        self._dataset = dataset
        self._coordinator = coordinator
        self._clock = clock or SimulationClock()
        self._current_index = 0
        self._state = ReplayState.STOPPED

    @property
    def state(self) -> ReplayState:
        return self._state

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def total_events(self) -> int:
        return len(self._dataset)

    @property
    def clock(self) -> SimulationClock:
        return self._clock

    @property
    def is_completed(self) -> bool:
        return self._current_index >= len(self._dataset)

    def step(self) -> Optional[MarketEvent]:
        """
        Advances replay by exactly one historical event.
        Advances simulation clock and feeds event synchronously to PipelineCoordinator.
        Returns the processed MarketEvent, or None if the dataset is exhausted.
        """
        if self._current_index >= len(self._dataset):
            self._state = ReplayState.COMPLETED
            return None

        self._state = ReplayState.STEPPING
        event = self._dataset[self._current_index]
        self._current_index += 1

        # Advance simulation clock
        self._clock.advance_to(event.exchange_timestamp)

        # Dispatch synchronously to frozen trading core pipeline
        self._coordinator.on_market_event(event)

        if self._current_index >= len(self._dataset):
            self._state = ReplayState.COMPLETED

        return event

    def seek(self, target_time: datetime) -> int:
        """
        Advances replay sequentially until reaching target_time.
        Every intermediate event is synchronously processed through the pipeline
        to maintain state, indicator, and position continuity.
        Returns the count of events processed during the seek.
        """
        if target_time is None:
            raise ValueError("seek() requires a non-null target_time.")

        if self._clock.current_time is not None and target_time < self._clock.current_time:
            raise ValueError(
                f"Cannot seek backwards from {self._clock.current_time.isoformat()} "
                f"to {target_time.isoformat()} without calling reset()."
            )

        events_processed = 0
        while self._current_index < len(self._dataset):
            next_event = self._dataset[self._current_index]
            if next_event.exchange_timestamp > target_time:
                break
            self.step()
            events_processed += 1

        return events_processed

    def play(self) -> int:
        """
        Executes unthrottled batch replay through the entire dataset.
        Zero sleep intervals; runs at maximum in-memory CPU throughput.
        Returns the total count of events processed.
        """
        self._state = ReplayState.RUNNING
        events_processed = 0

        while self._current_index < len(self._dataset):
            if self._state == ReplayState.PAUSED:
                break
            self.step()
            events_processed += 1

        if self._current_index >= len(self._dataset):
            self._state = ReplayState.COMPLETED

        return events_processed

    def pause(self) -> None:
        """Pauses the replay loop."""
        if self._state == ReplayState.RUNNING:
            self._state = ReplayState.PAUSED

    def reset(self) -> None:
        """Resets replay pointer and simulation clock to t=0."""
        self._current_index = 0
        self._clock.reset()
        self._state = ReplayState.STOPPED
