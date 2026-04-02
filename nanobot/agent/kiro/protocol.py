# @AI_GENERATED: Kiro v1.0
"""KiroProtocol: parse kiro-cli stdout (JSONL or plain text) and format stdin replies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Heuristic patterns for detecting interactive prompts in plain-text mode
_PROMPT_PATTERNS = [
    re.compile(r"\[y/n\]", re.IGNORECASE),
    re.compile(r"\[yes/no\]", re.IGNORECASE),
    re.compile(r"\(y/N\)", re.IGNORECASE),
    re.compile(r"\(Y/n\)", re.IGNORECASE),
    re.compile(r"请选择|请确认|请输入|please choose|please confirm", re.IGNORECASE),
    re.compile(r"continue\?\s*$", re.IGNORECASE),
    re.compile(r"proceed\?\s*$", re.IGNORECASE),
    re.compile(r":\s*$"),  # ends with colon (common input prompt)
]

_VALID_TYPES = {"output", "prompt", "done", "error", "file_changed"}


@dataclass
class KiroMessage:
    """Parsed message from kiro-cli stdout."""

    type: str  # output | prompt | done | error | file_changed
    content: str = ""
    prompt_id: str | None = None
    options: list[str] = field(default_factory=list)
    summary: str = ""
    exit_code: int | None = None
    path: str = ""
    action: str = ""
    raw: str = ""  # original line


def parse_line(line: str) -> KiroMessage:
    """Parse a single line from kiro-cli stdout.

    Tries JSONL first; falls back to plain-text heuristic.
    """
    stripped = line.strip()
    if not stripped:
        return KiroMessage(type="output", content="", raw=line)

    # Try JSONL
    if stripped.startswith("{"):
        try:
            data: dict[str, Any] = json.loads(stripped)
            msg_type = data.get("type", "output")
            if msg_type not in _VALID_TYPES:
                msg_type = "output"
            return KiroMessage(
                type=msg_type,
                content=data.get("content", ""),
                prompt_id=data.get("id"),
                options=data.get("options", []),
                summary=data.get("summary", ""),
                exit_code=data.get("exit_code"),
                path=data.get("path", ""),
                action=data.get("action", ""),
                raw=line,
            )
        except (json.JSONDecodeError, TypeError):
            pass  # fall through to plain-text

    # Plain-text fallback
    if is_interactive_prompt(stripped):
        return KiroMessage(type="prompt", content=stripped, raw=line)

    return KiroMessage(type="output", content=stripped, raw=line)


def is_interactive_prompt(text: str) -> bool:
    """Heuristic: does this text look like an interactive prompt?"""
    if not text:
        return False
    # Explicit question mark at end (but not in URLs or paths)
    if text.rstrip().endswith("?") and "://" not in text:
        return True
    return any(p.search(text) for p in _PROMPT_PATTERNS)


def format_reply(prompt_id: str | None, content: str, *, jsonl: bool = True) -> str:
    """Format a user reply for writing to kiro-cli stdin."""
    if jsonl:
        payload: dict[str, Any] = {"type": "reply", "content": content}
        if prompt_id:
            payload["prompt_id"] = prompt_id
        return json.dumps(payload, ensure_ascii=False) + "\n"
    # Plain-text mode: just send the text
    return content + "\n"
# @AI_GENERATED: end
