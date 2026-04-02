# Kiro Bridge 需求文档

## 概述

实现 nanobot 与 kiro-cli 之间的双向多轮交互桥接，让用户通过 nanobot 的 channel（Telegram/微信/Discord 等）发送编码任务给 kiro-cli 执行，kiro-cli 的交互式输出实时转发给用户，用户的回复路由回 kiro-cli。

## 参考文档

- #[[file:docs/KIRO_BRIDGE_DESIGN.md]]

## 需求

### 功能需求

1. **FR-1: Agent Tool 集成**
   nanobot 的 LLM agent 能通过 tool 调用启动 kiro-cli 任务，agent 自主决定何时将编码任务委托给 kiro。

2. **FR-2: 子进程管理**
   KiroBridge 能启动 kiro-cli 作为长驻子进程，通过 stdin/stdout 进行双向通信，并管理进程的完整生命周期（启动、运行、等待输入、完成、错误、超时）。

3. **FR-3: 输出实时转发**
   kiro-cli 的 stdout 输出实时转发到用户所在的 channel，支持两种模式：
   - JSONL 结构化协议（`output`/`prompt`/`done`/`error` 消息类型）
   - 纯文本降级模式（按行读取，启发式检测交互提示）

4. **FR-4: 用户回复路由**
   当 kiro-cli 处于等待用户输入状态时，用户在 channel 中的回复自动路由到 kiro-cli 的 stdin，而非走正常的 agent 处理流程。

5. **FR-5: 会话隔离**
   每个 session_key（channel:chat_id）独立管理各自的 kiro 会话，多用户并发使用互不干扰。

6. **FR-6: 任务完成通知**
   kiro-cli 任务完成后，通过 system message 通知 nanobot agent，agent 可以对结果进行汇总并回复用户。

7. **FR-7: 取消支持**
   用户可以通过发送 `/cancel` 或类似指令取消正在执行的 kiro 任务。

8. **FR-8: 配置化**
   kiro-cli 的路径、参数、超时时间等通过 config.json 配置，支持启用/禁用。

### 非功能需求

9. **NFR-1: 超时保护**
   - 执行超时：kiro-cli 运行超过配置时间（默认 600s）自动 kill
   - 输入超时：等待用户输入超过配置时间（默认 300s）自动取消并通知用户

10. **NFR-2: 错误恢复**
    kiro-cli 进程崩溃时自动检测，清理 session 状态，通知用户。

11. **NFR-3: 输出限制**
    单条消息超过 channel 允许的最大长度时自动分段发送。

12. **NFR-4: 最小侵入**
    对现有 nanobot 代码的修改尽量少，主要通过新增文件和少量路由逻辑集成。
