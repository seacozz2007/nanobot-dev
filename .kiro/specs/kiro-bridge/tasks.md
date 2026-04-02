# Kiro Bridge 实现任务

## 参考文档

- #[[file:docs/KIRO_BRIDGE_DESIGN.md]]
- #[[file:.kiro/specs/kiro-bridge/requirements.md]]
- #[[file:.kiro/specs/kiro-bridge/design.md]]

## 任务列表

### Phase 1: 核心基础设施

- [x] **Task 1: KiroSession 数据类与状态机**
  创建 `nanobot/agent/kiro/__init__.py` 和 `nanobot/agent/kiro/session.py`。
  实现 KiroSession dataclass，包含 process、session_key、channel、chat_id、state、pending_prompt_id、started_at、output_buffer、task 字段。
  state 类型为 `Literal["running", "waiting_input", "completed", "error"]`。
  相关文件：
  - #[[file:nanobot/agent/subagent.py]] （参考 subagent 的数据结构模式）

- [x] **Task 2: KiroProtocol 消息解析**
  创建 `nanobot/agent/kiro/protocol.py`。
  实现：
  - `parse_line(line: str) -> KiroMessage`：解析 JSONL 或纯文本
  - `KiroMessage` dataclass：type、content、prompt_id、options、summary、exit_code、path、action
  - `format_reply(prompt_id: str | None, content: str) -> str`：构造 stdin 回复
  - `is_interactive_prompt(text: str) -> bool`：纯文本模式下的启发式交互检测
  支持的消息类型：output、prompt、done、error、file_changed。
  纯文本降级：无法解析为 JSON 时，检测是否为交互提示，否则作为 output 处理。

- [x] **Task 3: KiroBridge 会话管理器**
  创建 `nanobot/agent/kiro/bridge.py`。
  实现 KiroBridge 类：
  - `__init__(bus, workspace, config)` — 初始化，持有 MessageBus 引用和配置
  - `start_task(session_key, task, channel, chat_id) -> str` — 启动 kiro-cli 子进程，创建 KiroSession，启动 _read_output 和 _watch_timeout 后台 task
  - `send_reply(session_key, content) -> bool` — 将用户回复通过 KiroProtocol.format_reply 写入 stdin，状态从 waiting_input 切回 running
  - `cancel(session_key) -> bool` — kill 进程，清理 session，通知用户
  - `has_active_session(session_key) -> bool`
  - `get_state(session_key) -> str | None`
  - `_read_output(session)` — 异步读取 stdout，通过 KiroProtocol 解析，按类型处理（output→转发，prompt→切状态+转发，done→完成，error→错误）
  - `_watch_timeout(session)` — 监控执行超时和输入超时
  - `_cleanup(session_key)` — 清理 session 映射
  - `_relay_text(channel, chat_id, content)` — 通过 MessageBus.publish_outbound 发送消息
  - `_announce_completion(session, result)` — 通过 system InboundMessage 通知 agent
  - `cleanup_all()` — 关闭所有活跃 session（用于 shutdown）
  相关文件：
  - #[[file:nanobot/agent/subagent.py]] （参考 _announce_result 模式）
  - #[[file:nanobot/bus/queue.py]] （MessageBus 接口）
  - #[[file:nanobot/bus/events.py]] （InboundMessage/OutboundMessage）

### Phase 2: 集成到 nanobot

- [x] **Task 4: KiroTool Agent 工具**
  创建 `nanobot/agent/tools/kiro.py`。
  实现 KiroTool(Tool)：
  - name: "kiro"
  - description: 描述何时使用此工具（编码任务委托）
  - parameters: task (string, required), workspace (string, optional)
  - execute: 调用 KiroBridge.start_task()，返回启动状态字符串
  - set_context(channel, chat_id): 设置当前消息上下文
  相关文件：
  - #[[file:nanobot/agent/tools/base.py]] （Tool 基类）
  - #[[file:nanobot/agent/tools/spawn.py]] （参考 SpawnTool 的 set_context 模式）

- [x] **Task 5: 配置 Schema 扩展**
  修改 `nanobot/config/schema.py`，新增 KiroConfig 类：
  - enabled: bool = False
  - command: str = "kiro"
  - args: list[str] = []
  - timeout: int = 600
  - input_timeout: int = 300
  - workspace: str | None = None
  将 KiroConfig 挂载到 ToolsConfig 中（`kiro: KiroConfig = Field(default_factory=KiroConfig)`）。
  相关文件：
  - #[[file:nanobot/config/schema.py]] （现有配置结构）

- [x] **Task 6: AgentLoop 集成**
  修改 `nanobot/agent/loop.py`：
  1. `__init__` 中：根据配置初始化 KiroBridge（如果 kiro.enabled）
  2. `_register_default_tools` 中：如果 KiroBridge 已初始化，注册 KiroTool
  3. `_set_tool_context` 中：为 kiro tool 也设置 context
  4. `_dispatch` 方法中：在现有逻辑之前，检查 `kiro_bridge.has_active_session(msg.session_key)`：
     - state == "waiting_input" → 调用 bridge.send_reply，return
     - state == "running" → 发送"正在执行中"提示，return
  5. `close_mcp` 或新增 shutdown 方法中：调用 bridge.cleanup_all()
  相关文件：
  - #[[file:nanobot/agent/loop.py]] （AgentLoop 类）
  - #[[file:nanobot/agent/tools/registry.py]] （ToolRegistry）

### Phase 3: 增强功能

- [x] **Task 7: 取消命令支持**
  修改 `nanobot/command/builtin.py`，注册 `/kiro_cancel` 命令（或在 _dispatch 中检测 `/cancel`）。
  调用 KiroBridge.cancel()，通知用户任务已取消。
  相关文件：
  - #[[file:nanobot/command/builtin.py]] （内置命令）
  - #[[file:nanobot/command/router.py]] （命令路由）

- [x] **Task 8: 输出分段与限流**
  在 KiroBridge._relay_text 中实现：
  - 单条消息超过 4096 字符时自动分段发送
  - 高频输出时合并（debounce），避免刷屏
  - 可配置的消息最大长度
