"""Shared, ROS-independent contracts for parsers, execution and adapters."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from threading import Event
from typing import Any, Callable, Protocol


class CommandError(ValueError):
    """An unsupported or invalid instruction; nothing should execute."""


class ExecutionError(RuntimeError):
    """An adapter could not complete an operation."""


class ExecutionCancelled(ExecutionError):
    """Cancellation acknowledged by an adapter."""


@dataclass(frozen=True)
class Step:
    kind: str
    target: str | None = None
    seconds: float = 0.0
    object_name: str = ""
    timeout: float = 60.0
    max_retries: int = 1
    step_id: str = ""
    condition: dict[str, Any] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    on_failure: str = "abort"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Plan:
    command: str
    steps: list[Step]
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command, "steps": [s.to_dict() for s in self.steps], "summary": self.summary,
                "metadata": self.metadata, "version": 3}


@dataclass(frozen=True)
class ParsedCommand:
    kind: str  # task, stop, pause, resume, status
    plan: Plan | None = None


@dataclass(frozen=True)
class PlanningResult:
    kind: str  # task/control/clarify/answer
    plan: Plan | None = None
    message: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    options: list[str] = field(default_factory=list)


Feedback = Callable[[dict[str, Any]], None]


class RobotAdapter(Protocol):
    """execute must honor cancellation and timeout; stop must be nonblocking."""

    mode: str

    def execute(self, step: Step, cancel: Event, feedback: Feedback) -> dict[str, Any]: ...

    def stop(self) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...

    def close(self) -> None: ...
