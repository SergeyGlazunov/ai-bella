"""Bounded Docker / observation timeouts for Task 8 (tunable without redesign)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimePolicy:
    """Operational budgets for inspect/info and writer observation."""

    docker_command_timeout_seconds: float = 120.0
    docker_transient_retries: int = 2
    docker_transient_pause_seconds: float = 1.0
    # Chunk for one `docker wait` / exec-inspect poll cycle. Expiry alone does
    # not give up: observation re-arms until writer_max_observe_seconds.
    writer_observation_budget_seconds: float = 300.0
    # Total time one writer stage is observed in the same night before
    # deferring (WriterStillAlive). Must cover real stage durations (UBD graph
    # FULL measured ~5.3 h). Running jobs are never killed. Overridable per
    # stand via runtime.writer_max_observe_seconds.
    writer_max_observe_seconds: float = 21600.0


DEFAULT_RUNTIME_POLICY = RuntimePolicy()


class WriteWindowClosed(RuntimeError):
    """A new write stage must not start; window closed or deferred."""

    def __init__(self, decision: str = "deferred_window") -> None:
        self.decision = decision
        super().__init__(decision)


class WriterStillAlive(RuntimeError):
    """Observation budget ended while the indexer is still running (not exit!=0)."""

    def __init__(self, message: str = "writer still alive") -> None:
        super().__init__(message)
