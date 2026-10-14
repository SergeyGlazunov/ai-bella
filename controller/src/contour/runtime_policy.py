"""Bounded Docker / observation timeouts for Task 8 (tunable without redesign)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimePolicy:
    """Operational budgets for inspect/info and writer observation."""

    docker_command_timeout_seconds: float = 30.0
    docker_transient_retries: int = 2
    docker_transient_pause_seconds: float = 1.0
    writer_observation_budget_seconds: float = 300.0


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
