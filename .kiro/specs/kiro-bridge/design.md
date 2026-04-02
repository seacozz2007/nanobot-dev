# Kiro Bridge 技术设计

## 参考文档

- #[[file:docs/KIRO_BRIDGE_DESIGN.md]]
- #[[file:.kiro/specs/kiro-bridge/requirements.md]]

## 架构

```
用户 ←→ Channel ←→ MessageBus ←→ AgentLoop ──→ KiroTool
                        ↑                           ↓
                        │                      KiroBridge
                        │                           ↓
                        └── relay output ←── kiro-cli (subprocess)
                            relay reply  ──→ stdin
```

## 组件设计

### 1. KiroSession（数据类 + 状态机）

文件：`nanobot/agent/kiro/session.py`

```python
@dataclass
class KiroSession:
    process: asyncio.subprocess.Process
    session_key: str
    channel: str
    chat_id: str
    state: Literal["running", "waiting_input", "completed", "error"]
    pending_prompt_id: str | None = None
    started_at: datetime
    output_buffer: list[str]
    task: str  # 原始任务描述
```

状态转换：
- `running` → `waiting_input`（收到 prompt 类型消息）
- `waiting_input` → `running`（用户回复后）
- `running` → `completed`（收到 done 消息或进程正常退出）
- `running` / `waiting_input` → `error`（进程崩溃或超时）

### 2. KiroProtocol（消息解析）

文件：`nanobot/agent/kiro/protocol.py`

职责：
- 解析 kiro-cli 的 stdout 输出（JSONL 或纯文本）
- 构造写入 stdin 的回复消息
- 纯文本模式下的交互提示启发式检测

JSONL 消息类型：
| type | 字段 | 说明 |
|------|------|------|
| `output` | content | 普通输出，直接转发 |
| `prompt` | id, content, options? | 需要用户输入 |
| `done` | summary, exit_code | 任务完成 |
| `error` | content, exit_code | 错误 |
| `file_changed` | path, action | 文件变更通知 |

纯文本降级检测规则：
- 以 `?` 结尾
- 包含 `[y/n]`、`[yes/no]`、`(y/N)` 等模式
- 包含 `请选择`、`请确认`、`Enter` 等关键词

### 3. KiroBridge（会话管理器）

文件：`nanobot/agent/kiro/bridge.py`

核心方法：
- `start_task(session_key, task, channel, chat_id)` → 启动 kiro-cli 子进程
- `send_reply(session_key, content)` → 将用户回复写入 stdin
- `cancel(session_key)` → 取消任务，kill 进程
- `has_active_session(session_key)` → 检查是否有活跃会话
- `get_state(session_key)` → 获取当前状态

内部机制：
- `_read_output()` — asyncio task，持续读取 stdout 并通过 MessageBus 转发
- `_watch_timeout()` — asyncio task，监控执行超时和输入超时
- `_cleanup()` — 进程退出后清理 session

### 4. KiroTool（Agent Tool）

文件：`nanobot/agent/tools/kiro.py`

Tool 定义：
- name: `kiro`
- parameters: `task` (string, required), `workspace` (string, optional)
- execute: 调用 KiroBridge.start_task()，返回启动状态

### 5. 配置 Schema

在 `nanobot/config/schema.py` 中新增：

```python
class KiroConfig(Base):
    enabled: bool = False
    command: str = "kiro"
    args: list[str] = ["--output-format", "jsonl"]
    timeout: int = 600
    input_timeout: int = 300
    workspace: str | None = None
```

挂载到 `ToolsConfig.kiro: KiroConfig`

### 6. AgentLoop 集成

修改 `nanobot/agent/loop.py`：

1. `__init__` 中初始化 KiroBridge（如果 config 启用）
2. `_register_default_tools` 中注册 KiroTool
3. `_dispatch` 中新增路由判断：活跃 kiro 会话 + waiting_input → 路由到 bridge

### 7. 消息流转详细

**启动任务：**
1. 用户发消息 → agent 决定调用 kiro tool
2. KiroTool.execute() → KiroBridge.start_task()
3. 启动 kiro-cli 子进程，创建 KiroSession
4. 启动 _read_output 和 _watch_timeout 后台 task
5. 返回 "Kiro 任务已启动" 给 agent

**输出转发：**
1. _read_output 读取 stdout 一行
2. KiroProtocol 解析消息类型
3. output → publish_outbound → Channel → 用户
4. prompt → 更新状态为 waiting_input → publish_outbound → 用户

**用户回复：**
1. 用户消息到达 _dispatch
2. 检测到 has_active_session + state == waiting_input
3. 调用 bridge.send_reply() → 写入 stdin
4. 状态切回 running

**任务完成：**
1. 收到 done 消息或进程退出
2. 状态设为 completed
3. 通过 system InboundMessage 通知 agent
4. 清理 session
