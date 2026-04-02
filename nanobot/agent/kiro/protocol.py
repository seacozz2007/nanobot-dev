# @AI_GENERATED: Kiro v1.0
"""KiroProtocol: parse kiro-cli stdout (JSONL or plain text) and format stdin replies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ── ANSI escape code stripper ───────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b\[[\d;]*m")

# ── Patterns for lines that are code-diff noise from kiro-cli ──
# Lines like: "+ 42": ..., "- 10": ..., or raw diff markers
_DIFF_LINE_RE = re.compile(
    r"^[+\-]\s*\d+\s*:"       # "+ 42:" or "- 10:" diff line numbers
)
_DIFF_HEADER_RE = re.compile(
    r"^(Creating|Reading|Editing|Deleting):\s"  # tool action headers
)
_TOOL_STATUS_RE = re.compile(
    r"^\s*[✓✗]\s+Successfully\s"               # "✓ Successfully read/wrote..."
)
_COMPLETED_RE = re.compile(
    r"^.*Completed in \d+\.\d+s\s*$"           # "- Completed in 0.2s"
)
_USING_TOOL_RE = re.compile(
    r"\(using tool:\s*\w+"                      # "(using tool: read, ...)"
)

# Heuristic patterns for detecting interactive prompts in plain-text mode
_PROMPT_PATTERNS = [
    re.compile(r"\[y/n\]", re.IGNORECASE),
    re.compile(r"\[yes/no\]", re.IGNORECASE),
    re.compile(r"\(y/N\)", re.IGNORECASE),
    re.compile(r"\(Y/n\)", re.IGNORECASE),
    re.compile(r"请选择|请确认|请输入|please choose|please confirm", re.IGNORECASE),
    re.compile(r"continue\?\s*$", re.IGNORECASE),
    re.compile(r"proceed\?\s*$", re.IGNORECASE),
]

_VALID_TYPES = {"output", "prompt", "done", "error", "file_changed"}


@dataclass
class KiroMessage:
    """Parsed message from kiro-cli stdout."""

    type: str  # output | prompt | done | error | file_changed | skip
    content: str = ""
    prompt_id: str | None = None
    options: list[str] = field(default_factory=list)
    summary: str = ""
    exit_code: int | None = None
    path: str = ""
    action: str = ""
    raw: str = ""  # original line


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _is_noise(text: str) -> bool:
    """Return True if the line is code-diff noise that should not be relayed."""
    if _DIFF_LINE_RE.match(text):
        return True
    if _TOOL_STATUS_RE.match(text):
        return True
    if _COMPLETED_RE.match(text):
        return True
    return False


def _extract_file_action(text: str) -> tuple[str, str] | None:
    """Try to extract a file action from tool headers like 'Creating: /path/to/file'."""
    m = _DIFF_HEADER_RE.match(text)
    if m:
        action = m.group(1).lower()  # creating, reading, editing, deleting
        rest = text[m.end():].strip()
        # Strip trailing "(using tool: ...)" and "- Completed in ..."
        rest = _USING_TOOL_RE.sub("", rest).strip()
        rest = _COMPLETED_RE.sub("", rest).strip()
        if rest:
            return action, rest
    return None


def _clean_agent_line(text: str) -> str:
    """Clean a kiro-cli agent output line (the '> ' prefixed lines)."""
    # Strip the "> " prefix that kiro-cli uses for agent speech
    if text.startswith("> "):
        text = text[2:]
    # Also strip leading/trailing whitespace
    return text.strip()


def parse_line(line: str) -> KiroMessage:
    """Parse a single line from kiro-cli stdout.

    Strips ANSI codes, filters diff noise, and extracts meaningful content.
    Tries JSONL first; falls back to plain-text heuristic.
    """
    # Step 1: strip ANSI escape codes
    cleaned = strip_ansi(line).strip()
    if not cleaned:
        return KiroMessage(type="skip", content="", raw=line)

    # Step 2: try JSONL
    if cleaned.startswith("{"):
        try:
            data: dict[str, Any] = json.loads(cleaned)
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

    # Step 3: filter diff/noise lines
    if _is_noise(cleaned):
        return KiroMessage(type="skip", content="", raw=line)

    # Step 4: detect file action headers (Creating: /path, Editing: /path)
    fa = _extract_file_action(cleaned)
    if fa:
        action, path = fa
        return KiroMessage(type="file_changed", content="", path=path, action=action, raw=line)

    # Step 5: detect "(using tool: ...)" lines — skip them
    if _USING_TOOL_RE.search(cleaned):
        return KiroMessage(type="skip", content="", raw=line)

    # Step 6: agent speech lines ("> some text")
    if cleaned.startswith("> "):
        text = _clean_agent_line(cleaned)
        if not text:
            return KiroMessage(type="skip", content="", raw=line)
        if is_interactive_prompt(text):
            return KiroMessage(type="prompt", content=text, raw=line)
        return KiroMessage(type="output", content=text, raw=line)

    # Step 7: plain text — check if it's a prompt
    if is_interactive_prompt(cleaned):
        return KiroMessage(type="prompt", content=cleaned, raw=line)

    # Step 8: remaining non-empty text is output
    return KiroMessage(type="output", content=cleaned, raw=line)


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
