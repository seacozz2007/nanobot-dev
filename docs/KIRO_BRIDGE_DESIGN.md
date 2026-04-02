# Kiro Bridge 设计方案：nanobot ↔ kiro-cli 多轮交互

## 1. 核心问题

用户通过 nanobot 的 channel（Telegram/微信/Discord 等）发送编码任务，nanobot 需要将任务委托给 kiro-cli 执行。kiro-cli 执行过程中可能需要用户确认、选择方案、提供额外信息等多轮交互，这些交互需要透传回 channel 给用户，用户的回复也需要路由回 kiro-cli。

## 2. 架构总览

```
用户 ←→ Channel (Telegram/微信/...) ←→ MessageBus ←→ AgentLoop
                                                         ↓
                                                    KiroBridge
                                                         ↓
                                              kiro-cli (长驻子进程)
                                              stdin ← 用户回复
                                              stdout → 实时输出 → Channel
```

## 3. 核心组件

### 3.1 KiroBridge（会话管理器）

位置：`nanobot/agent/kiro/bridge.py`

职责：
- 管理 kiro-cli 子进程的生命周期（启动、停止、超时回收）
- 维护 session_key → kiro 进程的映射
- 将 kiro-cli 的 stdout 实时转发到 MessageBus（→ Channel → 用户）
- 将用户的回复写入 kiro-cli 的 stdin
- 处理 kiro-cli 的状态机（空闲 / 执行中 / 等待用户输入）

### 3.2 KiroTool（Agent 工具）

位置：`nanobot/agent/tools/kiro.py`

职责：
- 作为 nanobot agent 的 tool，让 LLM 决定何时启动 kiro 任务
- 调用 KiroBridge 启动任务
- 返回初始状态给 agent

### 3.3 KiroRouter（消息路由）

集成位置：`nanobot/agent/loop.py` 的 `_dispatch` 方法

职责：
- 拦截用户消息，判断当前 session 是否有活跃的 kiro 交互
- 如果有，将消息路由到 KiroBridge 而非 AgentLoop
- 如果没有，走正常的 agent 处理流程

## 4. 状态机

```
                 ┌─────────┐
                 │  IDLE   │ ← 无活跃 kiro 进程
                 └────┬────┘
                      │ agent 调用 kiro tool
                      ▼
                 ┌─────────┐
                 │ RUNNING │ ← kiro-cli 正在执行
                 └────┬────┘
                      │ kiro 输出包含交互提示
                      ▼
              ┌───────────────┐
              │ WAITING_INPUT │ ← 等待用户回复
              └───────┬───────┘
                      │ 用户回复 → stdin
                      ▼
                 ┌─────────┐
                 │ RUNNING │ ← 继续执行
                 └────┬────┘
                      │ kiro-cli 退出
                      ▼
                 ┌──────────┐
                 │ COMPLETED│ → 结果通知 agent
                 └──────────┘
```

## 5. 通信协议

### 5.1 kiro-cli 输出协议

kiro-cli 需要支持结构化输出模式（`--output-format jsonl`），每行一个 JSON：

```jsonl
{"type": "output", "content": "正在分析代码结构..."}
{"type": "output", "content": "找到 3 个需要修改的文件"}
{"type": "prompt", "id": "confirm_1", "content": "是否继续修改以下文件？\n1. src/main.py\n2. src/utils.py\n3. tests/test_main.py", "options": ["yes", "no"]}
{"type": "output", "content": "开始修改..."}
{"type": "file_changed", "path": "src/main.py", "action": "modified"}
{"type": "done", "summary": "已完成 3 个文件的修改", "exit_code": 0}
{"type": "error", "content": "权限不足，无法写入文件", "exit_code": 1}
```

消息类型：
- `output`：普通输出，直接转发给用户
- `prompt`：需要用户输入，切换到 WAITING_INPUT 状态
- `file_changed`：文件变更通知（可选，用于 agent 感知）
- `done`：任务完成
- `error`：错误信息

### 5.2 用户回复路由

用户的回复通过 stdin 以 JSON 写入 kiro-cli：

```jsonl
{"type": "reply", "prompt_id": "confirm_1", "content": "yes"}
```

### 5.3 降级模式（纯文本）

如果 kiro-cli 不支持 jsonl 模式，bridge 也支持纯文本模式：
- stdout 按行读取，直接转发
- 检测交互提示的启发式规则（如以 `?` 结尾、包含 `[y/n]` 等）
- 用户回复直接写入 stdin + `\n`

## 6. 关键实现细节

### 6.1 KiroBridge 核心结构

```python
class KiroSession:
    """单个 kiro-cli 交互会话"""
    process: asyncio.subprocess.Process
    session_key: str          # channel:chat_id
    state: Literal["running", "waiting_input", "completed", "error"]
    pending_prompt_id: str | None
    started_at: datetime
    output_buffer: list[str]  # 累积输出，用于最终汇总

class KiroBridge:
    """管理所有 kiro 会话"""
    _sessions: dict[str, KiroSession]   # session_key → KiroSession
    _bus: MessageBus
    _workspace: Path
    _timeout: int = 600                 # 10 分钟超时

    async def start_task(session_key, task, channel, chat_id) -> str
    async def send_reply(session_key, content) -> bool
    async def cancel(session_key) -> bool
    def has_active_session(session_key) -> bool
    def get_state(session_key) -> str | None
```

### 6.2 消息路由逻辑（AgentLoop._dispatch 修改）

```python
async def _dispatch(self, msg: InboundMessage) -> None:
    # 新增：检查是否有活跃的 kiro 交互
    if self.kiro_bridge and self.kiro_bridge.has_active_session(msg.session_key):
        state = self.kiro_bridge.get_state(msg.session_key)
        if state == "waiting_input":
            # 用户回复路由到 kiro
            await self.kiro_bridge.send_reply(msg.session_key, msg.content)
            return
        elif state == "running":
            # kiro 正在执行，提示用户等待
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="⏳ Kiro 正在执行中，请稍候...",
            ))
            return

    # 原有逻辑
    lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
    ...
```

### 6.3 输出转发（stdout → Channel）

```python
async def _read_output(self, session: KiroSession, channel: str, chat_id: str):
    """持续读取 kiro-cli 输出并转发到 channel"""
    async for line in session.process.stdout:
        text = line.decode().strip()
        if not text:
            continue

        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            # 纯文本降级
            await self._relay_text(channel, chat_id, text)
            continue

        match msg.get("type"):
            case "output":
                await self._relay_text(channel, chat_id, msg["content"])
                session.output_buffer.append(msg["content"])

            case "prompt":
                session.state = "waiting_input"
                session.pending_prompt_id = msg.get("id")
                await self._relay_text(channel, chat_id, msg["content"])

            case "done":
                session.state = "completed"
                await self._announce_completion(session, channel, chat_id, msg)

            case "error":
                session.state = "error"
                await self._relay_text(channel, chat_id, f"❌ {msg['content']}")
```

### 6.4 超时与清理

- 每个 KiroSession 启动时注册一个超时 task
- WAITING_INPUT 状态超过 5 分钟自动取消，通知用户
- RUNNING 状态超过 10 分钟自动 kill
- 进程退出后自动清理 session 映射

### 6.5 完成后通知 Agent

kiro 任务完成后，通过 system message 通知 agent（复用 subagent 的 announce 模式）：

```python
async def _announce_completion(self, session, channel, chat_id, result_msg):
    """通知 agent kiro 任务已完成"""
    summary = result_msg.get("summary", "任务已完成")
    changed_files = session.output_buffer  # 或从 file_changed 事件收集

    announce = InboundMessage(
        channel="system",
        sender_id="kiro",
        chat_id=f"{channel}:{chat_id}",
        content=f"[Kiro 任务完成]\n\n结果: {summary}",
    )
    await self._bus.publish_inbound(announce)
```

## 7. 配置

在 `config.json` 中新增 kiro 配置段：

```json
{
  "tools": {
    "kiro": {
      "enabled": true,
      "command": "kiro-cli",
      "args": ["chat", "--no-interactive", "--trust-all-tools"],
      "timeout": 600,
      "input_timeout": 300,
      "workspace": null
    }
  }
}
```

## 8. 文件结构

```
nanobot/agent/kiro/
├── __init__.py
├── bridge.py       # KiroBridge - 会话管理器
├── protocol.py     # 消息解析（jsonl / 纯文本降级）
└── session.py      # KiroSession 数据类 + 状态机

nanobot/agent/tools/
└── kiro.py         # KiroTool - agent tool 接口
```

## 9. 交互流程示例

```
用户 (Telegram): "帮我重构 src/utils.py，把所有的 print 换成 logging"

nanobot agent: [决定使用 kiro tool]
  → KiroTool.execute(task="重构 src/utils.py...")
    → KiroBridge.start_task()
      → 启动 kiro-cli 子进程

kiro-cli stdout:
  {"type": "output", "content": "正在分析 src/utils.py..."}
  → Telegram: "正在分析 src/utils.py..."

  {"type": "output", "content": "发现 12 处 print 语句"}
  → Telegram: "发现 12 处 print 语句"

  {"type": "prompt", "id": "p1", "content": "其中 3 处在异常处理中，建议使用 logger.exception。其余使用 logger.info。\n确认方案？[yes/no/自定义]"}
  → Telegram: "其中 3 处在异常处理中..."
  → 状态切换: WAITING_INPUT

用户 (Telegram): "yes"
  → KiroBridge.send_reply("yes")
    → kiro-cli stdin: {"type": "reply", "prompt_id": "p1", "content": "yes"}
  → 状态切换: RUNNING

kiro-cli stdout:
  {"type": "file_changed", "path": "src/utils.py", "action": "modified"}
  {"type": "done", "summary": "已将 12 处 print 替换为 logging", "exit_code": 0}
  → Telegram: "已将 12 处 print 替换为 logging ✅"
  → 通知 agent 任务完成
```

## 10. 边界情况处理

| 场景 | 处理方式 |
|------|---------|
| kiro 进程崩溃 | 检测 returncode，通知用户并清理 session |
| 用户在 kiro 执行中发送无关消息 | 提示"Kiro 正在执行中"，或支持 `/cancel` 取消 |
| 多个用户同时使用 kiro | 每个 session_key 独立管理，互不干扰 |
| kiro 输出过长 | 分段发送，单条消息限制在 channel 允许的最大长度内 |
| 网络断开重连 | kiro 进程继续运行，重连后可查询状态 |
| 用户发送 `/cancel` | 调用 KiroBridge.cancel()，kill 进程 |

## 11. 实现优先级

1. **Phase 1**: KiroBridge 基础 + 纯文本模式（最小可用）
   - 启动/停止 kiro-cli 子进程
   - stdout 按行转发
   - 简单的交互检测（启发式）
   - 用户回复写入 stdin

2. **Phase 2**: JSONL 协议 + 状态机
   - 结构化消息解析
   - 完整状态机
   - 超时管理
   - agent 通知

3. **Phase 3**: 增强功能
   - 流式输出（streaming delta）
   - 文件变更追踪
   - 多 workspace 支持
   - kiro-cli MCP 模式迁移
