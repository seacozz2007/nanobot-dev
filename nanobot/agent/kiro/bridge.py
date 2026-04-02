# @AI_GENERATED: Kiro v1.0
"""KiroBridge: manages kiro-cli subprocess sessions for multi-turn interaction."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.kiro.protocol import KiroMessage, format_reply, parse_line
from nanobot.agent.kiro.session import KiroSession
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus


# Max characters per outbound message segment
_MAX_MSG_LEN = 4000
# Debounce interval for high-frequency output (seconds)
_DEBOUNCE_INTERVAL = 0.3


class KiroBridge:
    """Manage kiro-cli subprocess sessions bound to channel conversations."""

    def __init__(
        self,
        bus: MessageBus,
        workspace: Path,
        *,
        command: str = "kiro",
        args: list[str] | None = None,
        timeout: int = 600,
        input_timeout: int = 300,
    ) -> None:
        self._bus = bus
        self._workspace = workspace
        self._command = command
        self._args = args or []
        self._timeout = timeout
        self._input_timeout = input_timeout
        self._sessions: dict[str, KiroSession] = {}

    # ── public API ──────────────────────────────────────────────

    async def start_task(
        self,
        session_key: str,
        task: str,
        channel: str,
        chat_id: str,
        workspace: str | None = None,
    ) -> str:
        """Start a kiro-cli subprocess for the given task."""
        if session_key in self._sessions and self._sessions[session_key].is_active:
            return "Error: 当前会话已有一个活跃的 Kiro 任务，请等待完成或发送 /kiro_cancel 取消。"

        ws = workspace or str(self._workspace)
        cmd_args = [self._command] + self._args + [task]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ws,
            )
        except FileNotFoundError:
            return f"Error: 找不到 kiro-cli 命令 '{self._command}'，请确认已安装 Kiro CLI。"
        except Exception as e:
            return f"Error: 启动 kiro-cli 失败: {e}"

        session = KiroSession(
            process=process,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            task=task,
        )
        self._sessions[session_key] = session

        # Start background readers
        session._reader_task = asyncio.create_task(
            self._read_output(session)
        )
        session._timeout_task = asyncio.create_task(
            self._watch_timeout(session)
        )

        logger.info("Kiro task started for {}: {}", session_key, task[:80])
        return f"Kiro 任务已启动: {task[:100]}"

    async def send_reply(self, session_key: str, content: str) -> bool:
        """Send a user reply to the kiro-cli stdin."""
        session = self._sessions.get(session_key)
        if not session or not session.is_active:
            return False
        if session.state != "waiting_input":
            return False
        if session.process.stdin is None:
            return False

        # kiro-cli chat --no-interactive uses plain text I/O
        jsonl_mode = "--output-format" in " ".join(self._args)
        reply = format_reply(session.pending_prompt_id, content, jsonl=jsonl_mode)

        try:
            session.process.stdin.write(reply.encode())
            await session.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("Failed to write to kiro stdin for {}: {}", session_key, e)
            session.transition("error")
            await self._relay_text(session.channel, session.chat_id, "❌ Kiro 进程已断开。")
            await self._cleanup(session_key)
            return False

        session.transition("running")
        logger.debug("Reply sent to kiro for {}: {}", session_key, content[:80])
        return True

    async def cancel(self, session_key: str) -> bool:
        """Cancel an active kiro task."""
        session = self._sessions.get(session_key)
        if not session or not session.is_active:
            return False

        session.transition("error")
        try:
            session.process.kill()
        except ProcessLookupError:
            pass
        await self._relay_text(session.channel, session.chat_id, "🛑 Kiro 任务已取消。")
        await self._cleanup(session_key)
        logger.info("Kiro task cancelled for {}", session_key)
        return True

    def has_active_session(self, session_key: str) -> bool:
        session = self._sessions.get(session_key)
        return session is not None and session.is_active

    def get_state(self, session_key: str) -> str | None:
        session = self._sessions.get(session_key)
        return session.state if session else None

    async def cleanup_all(self) -> None:
        """Kill all active sessions (called on shutdown)."""
        keys = list(self._sessions.keys())
        for key in keys:
            session = self._sessions.get(key)
            if session and session.is_active:
                try:
                    session.process.kill()
                except ProcessLookupError:
                    pass
            await self._cleanup(key)

    # ── internal ────────────────────────────────────────────────

    async def _read_output(self, session: KiroSession) -> None:
        """Continuously read kiro-cli stdout and dispatch by message type."""
        try:
            while session.process.stdout and session.is_active:
                raw = await session.process.stdout.readline()
                if not raw:
                    break  # EOF — process exited
                line = raw.decode("utf-8", errors="replace")
                msg = parse_line(line)
                await self._handle_message(session, msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Error reading kiro output for {}: {}", session.session_key, e)
        finally:
            # Process ended — determine final state
            if session.is_active:
                rc = session.process.returncode
                if rc is None:
                    try:
                        await asyncio.wait_for(session.process.wait(), timeout=5.0)
                        rc = session.process.returncode
                    except asyncio.TimeoutError:
                        session.process.kill()
                        rc = -1

                if session.state not in ("completed", "error"):
                    if rc == 0:
                        session.transition("completed")
                        await self._announce_completion(
                            session, "Kiro 任务已完成。"
                        )
                    else:
                        session.transition("error")
                        # Read stderr for error details
                        err = ""
                        if session.process.stderr:
                            try:
                                err_bytes = await asyncio.wait_for(
                                    session.process.stderr.read(), timeout=2.0
                                )
                                err = err_bytes.decode("utf-8", errors="replace").strip()
                            except (asyncio.TimeoutError, Exception):
                                pass
                        err_msg = f"❌ Kiro 进程异常退出 (code={rc})"
                        if err:
                            err_msg += f"\n{err[:500]}"
                        await self._relay_text(session.channel, session.chat_id, err_msg)

                await self._cleanup(session.session_key)

    async def _handle_message(self, session: KiroSession, msg: KiroMessage) -> None:
        """Dispatch a parsed kiro message."""
        match msg.type:
            case "output":
                if msg.content:
                    session.output_buffer.append(msg.content)
                    await self._relay_text(session.channel, session.chat_id, msg.content)

            case "prompt":
                await self._flush_debounce(session)
                session.transition("waiting_input")
                session.pending_prompt_id = msg.prompt_id
                content = msg.content
                if msg.options:
                    content += "\n选项: " + " / ".join(msg.options)
                session.output_buffer.append(content)
                await self._relay_text(session.channel, session.chat_id, content, flush=True)

            case "done":
                await self._flush_debounce(session)
                session.transition("completed")
                summary = msg.summary or "任务已完成"
                await self._relay_text(
                    session.channel, session.chat_id, f"✅ {summary}", flush=True
                )
                await self._announce_completion(session, summary)

            case "error":
                await self._flush_debounce(session)
                session.transition("error")
                await self._relay_text(
                    session.channel, session.chat_id, f"❌ {msg.content}", flush=True
                )

            case "file_changed":
                if msg.path:
                    session.changed_files.append(msg.path)
                    await self._relay_text(
                        session.channel, session.chat_id,
                        f"📝 {msg.action or 'changed'}: {msg.path}",
                    )

    async def _watch_timeout(self, session: KiroSession) -> None:
        """Monitor execution and input timeouts."""
        try:
            while session.is_active:
                await asyncio.sleep(5)

                # Execution timeout
                if session.elapsed_seconds > self._timeout:
                    logger.warning("Kiro task timed out for {}", session.session_key)
                    session.transition("error")
                    try:
                        session.process.kill()
                    except ProcessLookupError:
                        pass
                    await self._relay_text(
                        session.channel, session.chat_id,
                        f"⏰ Kiro 任务超时 ({self._timeout}s)，已自动取消。",
                    )
                    await self._cleanup(session.session_key)
                    return

                # Input timeout
                ws = session.waiting_seconds
                if ws is not None and ws > self._input_timeout:
                    logger.warning("Kiro input timed out for {}", session.session_key)
                    session.transition("error")
                    try:
                        session.process.kill()
                    except ProcessLookupError:
                        pass
                    await self._relay_text(
                        session.channel, session.chat_id,
                        f"⏰ 等待回复超时 ({self._input_timeout}s)，Kiro 任务已取消。",
                    )
                    await self._cleanup(session.session_key)
                    return
        except asyncio.CancelledError:
            return

    async def _relay_text(self, channel: str, chat_id: str, content: str, *, flush: bool = False) -> None:
        """Buffer text and send after a short debounce to avoid flooding the channel."""
        if not content:
            return
        session = next(
            (s for s in self._sessions.values() if s.channel == channel and s.chat_id == chat_id),
            None,
        )
        if session is None or flush:
            # No session or explicit flush — send immediately
            await self._send_segments(channel, chat_id, content)
            return

        session._debounce_buf += ("\n" if session._debounce_buf else "") + content

        # Cancel any pending debounce flush and schedule a new one
        if session._debounce_task and not session._debounce_task.done():
            session._debounce_task.cancel()

        async def _flush() -> None:
            await asyncio.sleep(_DEBOUNCE_INTERVAL)
            text = session._debounce_buf
            session._debounce_buf = ""
            if text:
                await self._send_segments(channel, chat_id, text)

        session._debounce_task = asyncio.create_task(_flush())

    async def _flush_debounce(self, session: KiroSession) -> None:
        """Force-flush any pending debounce buffer."""
        if session._debounce_task and not session._debounce_task.done():
            session._debounce_task.cancel()
        text = session._debounce_buf
        session._debounce_buf = ""
        if text:
            await self._send_segments(session.channel, session.chat_id, text)

    async def _send_segments(self, channel: str, chat_id: str, content: str) -> None:
        """Split long messages and publish to the bus."""
        segments = [content[i:i + _MAX_MSG_LEN] for i in range(0, len(content), _MAX_MSG_LEN)]
        for seg in segments:
            await self._bus.publish_outbound(OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=seg,
            ))

    async def _announce_completion(self, session: KiroSession, summary: str) -> None:
        """Notify the agent that a kiro task has completed."""
        changed = ""
        if session.changed_files:
            changed = "\n变更文件: " + ", ".join(session.changed_files)

        announce_content = (
            f"[Kiro 任务完成]\n\n"
            f"任务: {session.task}\n"
            f"结果: {summary}{changed}\n\n"
            f"请简要告知用户任务结果。"
        )

        msg = InboundMessage(
            channel="system",
            sender_id="kiro",
            chat_id=f"{session.channel}:{session.chat_id}",
            content=announce_content,
        )
        await self._bus.publish_inbound(msg)

    async def _cleanup(self, session_key: str) -> None:
        """Clean up a session's background tasks and remove from registry."""
        session = self._sessions.pop(session_key, None)
        if not session:
            return
        for t in (session._reader_task, session._timeout_task, session._debounce_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        logger.debug("Kiro session cleaned up for {}", session_key)
# @AI_GENERATED: end
