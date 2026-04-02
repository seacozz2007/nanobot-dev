# @AI_GENERATED: Kiro v1.0
"""KiroSession: data class and state machine for a single kiro-cli interaction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

KiroState = Literal["running", "waiting_input", "completed", "error"]


@dataclass
class KiroSession:
    """A single kiro-cli interactive session bound to a channel conversation."""

    process: asyncio.subprocess.Process
    session_key: str  # channel:chat_id
    channel: str
    chat_id: str
    task: str  # original task description
    state: KiroState = "running"
    pending_prompt_id: str | None = None
    started_at: datetime = field(default_factory=datetime.now)
    waiting_since: datetime | None = None  # when state switched to waiting_input
    output_buffer: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    _reader_task: asyncio.Task | None = field(default=None, repr=False)
    _timeout_task: asyncio.Task | None = field(default=None, repr=False)
    _debounce_buf: str = field(default="", repr=False)
    _debounce_task: asyncio.Task | None = field(default=None, repr=False)

    def transition(self, new_state: KiroState) -> None:
        """Transition to a new state with validation."""
        valid = {
            "running": {"waiting_input", "completed", "error"},
            "waiting_input": {"running", "error"},
            "completed": set(),
            "error": set(),
        }
        if new_state not in valid.get(self.state, set()):
            return  # silently ignore invalid transitions
        self.state = new_state
        if new_state == "waiting_input":
            self.waiting_since = datetime.now()
        elif new_state == "running":
            self.waiting_since = None
            self.pending_prompt_id = None

    @property
    def is_active(self) -> bool:
        return self.state in ("running", "waiting_input")

    @property
    def elapsed_seconds(self) -> float:
        return (datetime.now() - self.started_at).total_seconds()

    @property
    def waiting_seconds(self) -> float | None:
        if self.waiting_since is None:
            return None
        return (datetime.now() - self.waiting_since).total_seconds()
# @AI_GENERATED: end
