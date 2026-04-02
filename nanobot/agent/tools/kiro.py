# @AI_GENERATED: Kiro v1.0
"""KiroTool: agent tool for delegating coding tasks to kiro-cli."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.kiro.bridge import KiroBridge


class KiroTool(Tool):
    """Tool to delegate coding tasks to Kiro IDE agent via multi-turn interaction."""

    def __init__(self, bridge: KiroBridge) -> None:
        self._bridge = bridge
        self._channel = "cli"
        self._chat_id = "direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current message context for routing kiro output."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "kiro"

    @property
    def description(self) -> str:
        return (
            "Delegate a coding task to Kiro IDE agent for interactive execution. "
            "Use this for code generation, refactoring, debugging, or complex coding tasks "
            "that benefit from IDE-level tooling. Kiro will execute the task and may ask "
            "the user for confirmation or choices during execution. "
            "Results are delivered directly to the user's channel."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The coding task to delegate to Kiro",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional workspace path override",
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, workspace: str | None = None, **kwargs: Any) -> str:
        """Start a kiro-cli task via the bridge."""
        session_key = f"{self._channel}:{self._chat_id}"
        return await self._bridge.start_task(
            session_key=session_key,
            task=task,
            channel=self._channel,
            chat_id=self._chat_id,
            workspace=workspace,
        )
# @AI_GENERATED: end
