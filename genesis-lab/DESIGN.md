# 设计说明：Genesis Lab

本文记录协议、投递语义、失败模型和取舍。面向要改代码或做实验分析的人。

---

## 1. 目标与非目标

### 目标

1. N >= 2 个 Claude Code agent 分别运行在互相隔离的 Docker 容器里；宿主机只有一个 Python 系统（supervisor）。
2. agent 知道 roster 中其他 agent 存在；唯一通信工具是 `agent_chat(recipient, message)`。
3. 消息由 supervisor 路由、持久化、可审计；可以测出每条消息的端到端延迟。
4. 收消息的 agent **不被打断**：不在模型生成中途取消或修改请求。
5. 在“允许注入的最早安全点”交付消息。
6. 没有 API key 也能用脚本化 mock 跑通完整链路。

### 非目标

- 不是对抗性多租户沙箱；
- 不做模型层面的 token 级实时打断（API 不支持）；
- 不做崩溃自动重启/会话恢复；
- 不做多机/多进程 broker 高可用；
- 不定义 agent 之间的结构化协议（本轮只做自由文本 + 系统消息）。

---

## 2. 角色与边界

```
宿主机
  supervisor (Python)
    - CLI：init / up / down / status / say / end / logs / report
    - broker：唯一路由者，持有 run token
    - webconsole：可选（up --web），本地控制台 / 事件 tail
    - 事件存储：runs/<id>/events.jsonl
  Docker Engine
    agent-a  [net-a]
      runtime.bridge.py        ← 只与宿主机 broker 通信
        claude (stream-json)   ← 唯一被 bridge 写 stdin 的进程
          MCP server (stdio)   ← agent_chat 工具
          PostToolBatch hook   ← 本地 HTTP 到 bridge
    agent-b ... agent-n  [net-b ... net-n]   同样的结构
```

消息路径：

```
A 的模型 → MCP agent_chat → A.bridge → host.broker
                                      ↓ 分配 id/seq、写日志
                                   B 的 SSE
                                      ↓
                               B.bridge inbox
                                      ↓ 安全点
                          B 的 hook additionalContext / 新 user turn
                                      ↓
                               B 的模型
```

容器内 bridge **不做路由**：它只负责 A 的 Claude 进程监护、hook 端点、把消息转给宿主机。这样可以保证“system”始终在宿主机 Python 里。

---

## 3. 生命周期与创世

### 3.1 注册与 barrier

- 每个 bridge 启动后先 `POST /v1/agents/{id}/register`，拿到：
  - 自己的身份、完整 peers 列表；
  - 静态 `world_prompt`（world.md）；
  - 消息长度配置。
- 然后建立 `GET /v1/agents/{id}/stream?since=N`（SSE）长连接。
- broker 只有在 **全部 agent 都 registered 且 connected** 后才进入 `running`，并同时向所有 stream push `genesis` 事件。
- genesis 事件里是**已经渲染好的完整 prompt**（身份、peers、agent_count、mission、run id、时间）；每个 agent 的差异是身份和 peers 列表。
- bridge 收到 genesis 后才启动 Claude Code；第一条 stdin 消息就是 genesis prompt。

为什么要 barrier：避免 A 先诞生、先说话，而 B 还不存在导致消息堆积/丢失；保证“创世纪”是同一时刻。

### 3.2 prompt 分层

```
Claude Code 默认 system prompt
  + --append-system-prompt world.md         （静态世界规则/通信规则）
  + 第一条 user message = genesis.md 渲染     （身份、peers、agent_count、mission、run id）
```

world.md 是静态的，有利于 prompt cache；genesis 是每个 agent 不同的。mission 只出现在 genesis 里，修改 mission 不需要重建镜像。

### 3.3 结束

```
request_end(reason)
  → phase=ending
  → 给所有 agent 发系统消息 "The supervisor is ending ..."
  → 等待 END_GRACE_SECONDS（默认 45s）
  → 向所有 SSE push shutdown 事件
  → 每个 bridge 在下一个安全点交付剩余消息 → 收尾 → 退出
  → orchestrator 等待全部离线 / 超时 → docker compose down
  → finalize：写 run_ended、transcript.md、metrics.json
```

`END_GRACE_SECONDS` 既要给 agent 收尾，也要保证消息（最终通知）能进入上下文。

---

## 4. 投递语义（核心）

### 4.1 物理约束

Claude Code 的一次回合 = 一个或多个模型请求 + 工具执行。一个模型请求一旦发出：

- 不能在生成中途 append token；
- 只能取消（interrupt）→ 本回合失败/丢弃；
- 或者等这个请求返回、工具执行完，进入下一个请求。

所以“尽早收到提醒”只能在**请求之间的安全点**实现。

### 4.2 两个安全点

| 点 | 时机 | 实现 | 是否打断 |
| --- | --- | --- | --- |
| 工具批次后 | 工具结果已有、下一个模型请求未发 | `PostToolBatch` hook 返回 `hookSpecificOutput.additionalContext` | 否 |
| 回合结束后 | `result` 事件 | bridge 写一行 `{"type":"user",...}` 到 claude stdin | 否（此时无进行中回合） |

注意：hook 是同步的——Claude Code 会等 hook 返回，再把 `additionalContext` 包成 system reminder 插入上下文。我们返回得快（本地 HTTP，毫秒级），不会显著拖慢回合。

如果 agent 在一个长回合里**一次工具都没调用**，那么消息只能等 `result`；这就是最坏延迟。没有破坏这个约束的“魔法”；任何声称在生成中途注入的方案，本质上都在取消或重启请求。

### 4.3 状态机

```
            genesis
  starting ─────────► working ──result──► idle
                         ▲                 │
                         │  stdin user msg │
                         └─────────────────┘

  working: 允许 hook 交付；禁止写 stdin
  idle:    禁止 hook 交付（hook 不会触发）；允许写 stdin 唤醒
  ending:  收尾宽限期，允许最后几条消息；shutdown 后 bridge 拒绝新消息
```

bridge 用一个 asyncio 锁串行化“从 inbox 取消息”和“写 stdin”，避免两条路径同时消费同一批消息。

### 4.4 不变量

- **I1**：Claude 的 stdin 只在 `state == idle` 时写入（genesis 例外：进程刚启动、尚无回合）。
- **I2**：回合内的唯一投递路径是 PostToolBatch hook；hook 只读取 `state == working` 下的 inbox。
- **I3**：取走消息与生成 hook 响应在同一临界区完成；响应返回后异步 ack（channel=hook）。
- **I4**：idle 路径取走消息后立即写 stdin 并把状态置为 working，然后 ack（channel=turn）。
- **I5**：broker 对每个接收方维护严格递增 `seq`；bridge 按 seq 顺序消费；inbox 按 `message_id` 去重。
- **I6**：单条消息在 broker 入口限长（默认 4000）；一次 hook 注入按预算打包（默认 8000，低于 Claude Code 10000 硬上限）；超出的留在 inbox，下一个安全点继续。
- **I7**：bridge 是 Claude stdin 的唯一写入者；MCP server 和 hook 都不写 stdin。
- **I8**：注入文本明确写成“supervisor 路由的数据，不是操作员指令”，避免被模型当成系统命令，同时保留 peer 消息的可读性与发送方名字。

### 4.5 一次投递的伪代码

```python
# 回合中：由 PostToolBatch hook 调起
async def handle_hook(payload):
    if state != "working": return {}
    async with delivery_lock:
        if state != "working" or not inbox: return {}
        msgs = inbox.take(hook_batch_max_chars)      # 按序、按预算
        text = format_delivery(msgs, channel="hook")
        schedule_ack(msgs, channel="hook", turn=current_turn)
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolBatch",
        "additionalContext": text,
    }}

# 回合结束：由 stdout 的 result 事件调起
async def on_turn_end(result):
    state = "idle"
    await broker.post_state("idle", turn)
    async with wake_lock:
        if state != "idle" or not inbox: return
        msgs = inbox.take(hook_batch_max_chars)
        await harness.send_user(format_delivery(msgs, channel="turn"))
        state = "working"
        schedule_ack(msgs, channel="turn", turn=current_turn)
```

### 4.6 延迟分解

对每条消息：

```
sent_at          broker 收到并分配 id 的时刻
received_at      bridge 的 SSE 收到并放入 inbox 的时刻
delivered_at     bridge 把消息交给 hook 响应 / 写 stdin 的时刻（ack 时间）

network_s = received_at - sent_at     网络与 SSE 传输
latency_s = delivered_at - sent_at    端到端（含等待安全点）
```

`metrics.json` 会给出 p50/p95/max。research 分析时应按 channel 分开看：hook 和 turn 的等待原因不同。

### 4.7 排序、去重、重连

- broker 的每条消息有全局 id（`m000012` / `s000003`）和**每个接收方独立**的 seq（SSE 的 `id:`）。
- bridge 重连时带 `since=last_seq`，broker 从 history 重放之后的事件。
- bridge 的 inbox 记得所有见过的 message_id，因此重放不会重复注入。
- 若在“取走消息”之后、hook 返回之前进程崩溃，这条消息会丢（at-most-once per context）。这是有意的取舍：不为了 exactly-once 阻塞 hook，从而拖慢模型回合。broker 的 `delivered` 记录让你能发现未 ack 的消息。
- 若 ack 本身失败，broker 会认为未投递，重连后重放；bridge 因为已经见过 id，不会二次注入。研究时以 `message_delivered` 事件为准。

### 4.8 为什么不用 interrupt / 轮询

- **interrupt**：会取消当前模型请求，丢失正在生成的推理和工具计划；与“不打断”直接冲突。
- **agent 轮询 inbox 工具**：需要 agent 主动调用，浪费回合；而且不解决“它正在长回合里”的问题。hook 是零配合的。
- **SDK 持续 stdin**：Claude Agent SDK 的 streaming input 也只是把 user message 排队到下一次回合/请求之间；它不能在中途插入。而且那样会把 Claude 的进程控制放到宿主机，跨容器更复杂。当前方案把进程控制留在容器内，host 仍是纯 system。
- **共享卷**：会绕开 supervisor 的路由和记账，破坏实验可控性；所以不共享卷。
- **直接容器互联**：同样绕开 system；所以每个 agent 一个独立 network。

### 4.9 为什么 hook 用 `PostToolBatch`

- 每个工具批次一次，而不是每个工具一次；并行工具不会重复注入。
- 恰好在“工具结果已产生、下一次模型请求未发出”的位置。
- 对 MCP 工具同样生效（真实 Claude CLI 集成测试覆盖了 `mcp__agentchat__agent_chat` 后的 hook）。
- 比 `Stop`（只在回合结束）更早；比 `PreToolUse`（工具执行前，但注入内容要等结果一起给模型）语义更自然。

Claude Code 的 hook 失败策略：我们让 hook 永远快速返回 `{}` 或合法 JSON；即使 bridge 挂了，脚本也会吞掉异常并输出 `{}`，不会阻塞 agent 回合。

---

## 5. 协议与数据模型

### 5.1 HTTP / SSE

| 端点 | 调用方 | 作用 |
| --- | --- | --- |
| `POST /v1/agents/{id}/register` | bridge | 注册，拿 world prompt 与配置 |
| `GET /v1/agents/{id}/stream?since=N` | bridge | SSE：`genesis` / `message` / `shutdown` |
| `POST /v1/agents/{id}/state` | bridge | working / idle / starting / offline + turn |
| `POST /v1/agents/{id}/delivered` | bridge | 消息真正注入后的回执 |
| `POST /v1/agents/{id}/trace` | bridge | 回合轨迹（assistant 文本、tool_use、usage） |
| `POST /v1/agents/{id}/messages` | bridge（MCP 代理） | 发送 `agent_chat` |
| `POST /v1/system/messages` | host CLI | 操作者注入系统消息 |
| `POST /v1/end` | host CLI / orchestrator | 优雅结束 |
| `GET /v1/status` | host CLI | 状态快照 |

鉴权：host 持有 run token；每个容器只持有 `HMAC(run_token, "agent:<id>")`。容器 token 只能访问自己 id 的路径；`/v1/system/messages`、`/v1/end`、`/v1/status` 只允许 host token。

### 5.2 消息

```json
{
  "id": "m000012",
  "seq": 7,
  "sender_id": "a",
  "sender": "agent-a",
  "recipient_id": "b",
  "recipient": "agent-b",
  "kind": "chat",            // chat | system
  "text": "...",
  "sent_at": 1792295000.123,
  "sent_at_iso": "2026-10-18T03:43:20.123Z"
}
```

- `seq` 是接收方 SSE 的序号；`id` 是全局消息号。
- `system` 消息由 supervisor 发出（sender=supervisor），走同一条投递通道。
- 发送方只能指定 roster 中的其他 agent（名字或 id）；broker 会拒绝自己、未知名字。

### 5.3 存储

- `events.jsonl`：append-only，逐行 flush；崩溃可恢复分析。
- `messages.jsonl`：report 时把消息和投递记录 join 后导出。
- `transcript.md`：人类可读对话。
- `metrics.json`：聚合指标。
- `run.json`：prompt、配置（密钥脱敏）、限制、镜像、启动模式。

---

## 6. 隔离与信任边界

### 已实现

- N 个容器各自一个 bridge network，互不可达，也无共享卷。
- 每个容器一个独立派生 broker token；host 有 run token。
- Claude 子进程环境里移除 `AGENT_TOKEN` / `BROKER_TOKEN` / `BROKER_URL`；MCP 只用本地 bridge token。
- MCP/hook 只监听 `127.0.0.1` 的随机端口；agent 不能从容器外访问。
- 容器内 bridge 只允许注册/收发自己 id 的消息；broker 校验 recipient 属于当前 roster 且不是自己。

### 残余风险（务必知道）

1. **API key 在容器内**：Claude Code 进程需要它，agent 若有 Bash/Read，理论上可读到自己的环境。建议用 scoped key。
2. **/proc 读取**：bridge 与 Claude 同用户时，agent 可读父进程 environ。真正对抗场景应把 bridge 放到另一个 UID，或做 key proxy。
3. **网络出站**：容器需要访问 LLM API，所以有互联网出口；agent 理论上可以用 Bash/WebFetch 访问外部。broker 层无法完全禁止。需要严格隔离时，加 egress proxy 并禁用网络工具。
4. **Bash 工具**：本实验保留 Bash（便于任务型实验）。如果只关心聊天，可在 `runtime/harness.py` 的 `DISALLOWED_TOOLS` 中加入 `Bash`。

这些是研究实验的合理默认值，不是生产级安全边界。

---

## 7. 可复现性与度量

- 镜像固定 Claude Code 版本（`@anthropic-ai/claude-code@2.1.296`）。
- 每个 run 的模型、prompt、限制、镜像、broker 端口和 agent roster 写入 `run.json`；密钥脱敏。
- session id 由 `uuid5(run_id, agent_id)` 确定性生成。
- `agent_trace` 记录每回合 usage/cost、assistant 文本、tool_use 名称与参数预览；不记录完整 tool result（体积与敏感性）。
- `metrics.json` 的 cost 来自 Claude Code 的 `total_cost_usd`；mock 模式下是虚构值。
- 延迟是 wall-clock，包含宿主机调度；对跨机器对比需谨慎。

---

## 8. 关键常量

| 常量 | 值 | 位置 |
| --- | --- | --- |
| additionalContext 硬上限 | 10000 字符 | Claude Code 文档 |
| hook 注入预算 | 8000 字符 | `HOOK_BATCH_MAX_CHARS` |
| 单条消息上限 | 4000 字符 | `MAX_MESSAGE_CHARS` |
| SSE 心跳 | 15s | `broker.h_stream` / client |
| 离线判定宽限 | 15s | `broker.OFFLINE_GRACE_SECONDS` |
| 默认收尾宽限 | 45s | `END_GRACE_SECONDS` |
| shutdown 硬截止 | 90s | `broker._ending_sequence` |
| 注册/创世超时 | 900s（bridge）/ 180s（orchestrator） | 见代码 |

---

## 9. 后续可扩展

- **key proxy**：容器只拿假 token，host 代理真实的 DeepSeek 请求；彻底解决 key 可见性。
- **崩溃恢复**：run token + since 重放 + Claude `--resume`；目前刻意不做。
- **N 个 agent**：把 `agents` 从两元组改成图；agent_chat 的 recipient 校验改成白名单；隔离网络按 agent 复制。消息格式/seq 已经是 per-recipient，扩展成本低。
- **结构化消息**：在 `agent_chat` 加 `reply_to` / `type`；broker 仍是唯一路由。
- **更强的可观测**：把 trace 接到 OpenTelemetry；记录每次模型请求的 token 与延迟。
- **更严格的静态工具集**：去掉 Bash/Web，仅保留 agent_chat + 只读文件，做纯对话实验。

---

## 10. 代码地图（按阅读顺序）

1. `supervisor/broker.py`：世界状态、创世、路由、ack、结束。
2. `runtime/bridge.py`：状态机、hook 交付、idle 唤醒、收尾。
3. `runtime/formatting.py`：注入文本长什么样。
4. `runtime/harness.py`：Claude Code 命令行、hook/MCP 配置、stdout 解析。
5. `supervisor/cli.py`：up 的编排与终端。
6. `supervisor/mock_llm.py`：无 key 的干跑与集成测试。
7. `tests/test_cli_integration.py` / `tests/test_two_agent_world.py`：真实 CLI 的端到端验证。
