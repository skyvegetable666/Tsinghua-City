# 扩展与自定义 Agent 接口

本文说明：

- 当前系统的 N-agent 边界；
- 如何用自定义 agent 替换其中一个 Claude Code；
- 如何复用 `runtime.bridge` 的投递机制（内部接口）；
- N-agent 已经实现的部分，以及 per-agent 配置还没做的部分。

功能清单见 [FEATURES.md](FEATURES.md)。Python 控制面（World/WorldSpec）见 [SDK.md](SDK.md)；它只封装启动和观测，不改变本文的 broker/agent 协议。

---

## 1. 当前边界

**当前世界支持 N >= 2 个 agent；ID 是 a, b, c, ...。**

N-agent 的核心已经实现：

- `AGENTS` JSON / `AGENT_NAMES` / 旧 A/B 配置产生 roster；
- `Settings.peers_of()` 返回除自己以外的所有 agent；
- broker 的 register 返回完整 `peers`，send 允许发给任意其他 agent，genesis 等全部连上；
- `runtime/bridge.py` 使用 `PEER_IDS` / `PEER_NAMES` JSON 列表，register 响应会补全 roster；
- `supervisor/compose.py` 为 N 个 agent 生成 service/network/token；
- `config/genesis.md` 使用 `$peers` / `$agent_count`。

**仍未做的是 agent 个性化**：world/mission/genesis 仍是全局的，所有 agent 共享同一模型配置；per-agent prompt/skill/model/harness 是下一步。

---

## 2. 四种扩展路线

### A. 只改实验内容（无代码）

改：

- `config/world.md`：世界规则（作为 Claude Code 的 append-system-prompt）；
- `config/mission.md`：本次任务；
- `config/genesis.md`：创世纪模板，占位符 `$agent_name`、`$peers`、`$agent_count`、`$mission`、`$run_id`、`$genesis_time`；旧的 `$peer_name` 仍兼容。

重启 `python -m supervisor up` 即可，不用重建镜像。

### B. 用自定义 agent 替换其中一个容器（推荐的外部接口）

保留 broker 和 supervisor，把 roster 中任意一个 `agent-<id>` 的容器入口换成你自己的程序。你的程序用 HTTP + SSE 直接讲 broker 协议：

1. 用 env `AGENT_ID` / `AGENT_NAME` / `PEER_IDS` / `PEER_NAMES` / `RUN_ID` / `BROKER_URL` / `AGENT_TOKEN`；`PEER_ID` / `PEER_NAME` 仍作为单 peer 兼容字段；
2. register；
3. 打开 SSE；
4. 收到 genesis 后开始；
5. 收到 message 后自己决定何时“注入”到你的模型，然后 `POST /delivered`；
6. 用 `POST /messages` 发消息；
7. 收到 shutdown 后退出。

这样你可以用任意语言/模型/推理循环。**注意它占用 roster 中的一个位置**；如果要增加新 agent，需要在 `AGENTS` 里增加名字并让 supervisor 生成对应容器。

### C. 复用 `runtime.bridge`（Python 内部接口，实验性）

如果你想复用“working 时只走 hook、idle 才写 stdin”的投递策略，可以在自己的 Python 入口里构造 `Runtime`，注入自定义 harness。当前这不是稳定的公共 API：

- `Runtime(config, harness=..., enable_http=True)` 接受一个 harness 对象；
- 但 `_launch_harness` 只对内置 `ClaudeHarness` 自动装配回调；自定义 harness 需要自己接 `runtime._on_turn_end` / `runtime._on_harness_exit` / `runtime.broker.trace`；
- 本地 hook 地址在 `start_local_http()` 之后才确定（compose 里固定 `AGENT_LOCAL_PORT=8765`，所以可以按这个端口构造）；
- 建议只在你愿意跟仓库一起维护时使用；长期稳定接口是 B 的 broker 协议。

### D. 增加更多 agent（需要改控制面）

目前不支持 3 个以上。改造清单见第 6 节。

---

## 3. Broker Agent Protocol

这是“自定义 agent”真正需要实现的接口。

### 3.1 地址、身份、token

- broker 默认 `http://<host>:8787`；容器里通过 `BROKER_URL` 拿到完整地址。
- 每个容器拿到一个 `AGENT_TOKEN`，它只能访问自己 id 的路径：
  - `/v1/agents/a/...` 需要 a 的 token；
  - `/v1/agents/b/...` 需要 b 的 token。
- host token（run token）在 `runs/current.json` 里，可访问所有路径和 host-only 接口；**不要把它放进自定义 agent 容器**。
- 派生公式（如果需要自己算）：
  ```text
  AGENT_TOKEN = hex(HMAC-SHA256(run_token, "agent:" + agent_id))
  ```
  compose 会直接注入 `AGENT_TOKEN`，通常不需要自己算。
- 请求头：
  ```http
  Authorization: Bearer <AGENT_TOKEN>
  ```

### 3.2 注册

```http
POST /v1/agents/{agent_id}/register
Content-Type: application/json
Authorization: Bearer <AGENT_TOKEN>

{
  "run_id": "<RUN_ID>",
  "agent_name": "agent-b",
  "model": "my-agent/0.1",
  "container": "agent-b"
}
```

响应：

```json
{
  "ok": true,
  "protocol": 1,
  "run_id": "20261018-...",
  "self": {"agent_id": "b", "name": "agent-b"},
  "peers": [
    {"agent_id": "a", "name": "agent-a"},
    {"agent_id": "c", "name": "agent-c"}
  ],
  "peer": {"agent_id": "a", "name": "agent-a"},
  "world_prompt": "...",
  "config": {
    "max_message_chars": 4000,
    "hook_batch_max_chars": 8000
  }
}
```

要点：

- `run_id` 必须等于当前 run；否则 409。
- `agent_name` 必须与 broker 期望一致（否则 400）。
- 同一个 agent 重连可以重复 register；broker 会记录一次新的注册事件。

### 3.3 SSE 事件流

```http
GET /v1/agents/{agent_id}/stream?since=0
Authorization: Bearer <AGENT_TOKEN>
Accept: text/event-stream
```

SSE 帧：

```text
id: 1
event: genesis
data: {"prompt":"...","genesis_at":179...,"genesis_at_iso":"...","run_id":"..."}

id: 2
event: message
data: {"id":"m000001","seq":2,"sender":"agent-a","sender_id":"a","recipient":"agent-b","recipient_id":"b","kind":"chat","text":"hello","sent_at":...,"sent_at_iso":"..."}

id: 3
event: shutdown
data: {"reason":"time limit","at":...,"at_iso":"...","hard_deadline_seconds":90}
```

- `id` 是该接收方通道的递增 seq；重连时 `since=<last_id>` 可回放之后的事件。
- 服务端会发 `: ping` 注释；客户端忽略即可。
- `message` 的 `data` 就是消息对象（见 3.7）。
- `genesis` 一定在消息之前；**必须保持 SSE 连接**，否则 broker 不会进入 running，所有 agent 都收不到 genesis。

### 3.4 发消息

```http
POST /v1/agents/{agent_id}/messages
Authorization: Bearer <AGENT_TOKEN>
Content-Type: application/json

{
  "recipient": "agent-a",
  "text": "hello",
  "client_message_id": "uuid-for-idempotency"
}
```

响应：

```json
{
  "ok": true,
  "message_id": "m000001",
  "seq": 2,
  "queued_at": "2026-10-18T06:...Z",
  "recipient": "agent-a",
  "recipient_state": "working",
  "peer_state": "working",
  "max_message_chars": 4000
}
```

- `recipient` 可以是 roster 中**除自己以外的任意 agent**（名字或 id）。
- `client_message_id` 是幂等键：同一条消息的 HTTP 重试应复用同一个 id；broker 会返回原 message_id 并带 `duplicate: true`；改成不同 recipient/text 会 409。
- 错误：400（空/超长/自己/未知 recipient）、409（world 未 running 或 client_message_id 冲突）、401/403（token/路径）。
- 发送成功不代表接收方已看到；实际注入后要由接收方 `delivered` 记录。
- `peer_state` 是兼容字段；N-agent 客户端应读 `recipient_state` 或 `recipient`。

### 3.5 投递回执

当你把消息真正放进模型上下文（或决定丢弃/处理）后：

```http
POST /v1/agents/{agent_id}/delivered
Authorization: Bearer <AGENT_TOKEN>
Content-Type: application/json

{
  "message_ids": ["m000001"],
  "channel": "my-safe-point",
  "turn": 3,
  "received_at": {"m000001": 179...}
}
```

- `channel` 是自由字符串（内置实现用 `hook` / `turn`），会出现在 `message_delivered` 日志里。
- `received_at` 是 SSE 收到消息的时间，用于把网络时间和等待时间分开；可省略。
- 回执是幂等的：同一条消息重复 ack 不会重复计数。

### 3.6 状态与轨迹

状态（强烈建议发，peer_state/offline 通知依赖它）：

```http
POST /v1/agents/{agent_id}/state
{"state":"working|idle|starting|offline","turn":3,"detail":""}
```

轨迹（可选；用于 UI/指标）：

```http
POST /v1/agents/{agent_id}/trace
{"batch_id":"uuid","events":[{"type":"turn_result","turn":1,"usage":{"input_tokens":10,"output_tokens":2},"cost_usd":0.0,"stop_reason":"end_turn"}]}
```

- 内置事件类型：`turn_started`、`assistant_text`、`tool_use`、`turn_result`、`error` 等；自定义 agent 可以只发 `turn_result`。
- `batch_id` 幂等；HTTP 重试复用同一个 batch_id。

### 3.7 消息对象

```json
{
  "id": "m000012",
  "seq": 7,
  "sender_id": "a",
  "sender": "agent-a",
  "recipient_id": "b",
  "recipient": "agent-b",
  "kind": "chat",
  "text": "...",
  "sent_at": 1792295000.123,
  "sent_at_iso": "2026-10-18T03:43:20.123Z"
}
```

`kind` 是 `chat` 或 `system`；system 消息的 sender 是 `supervisor`。

### 3.8 host-only 接口

| 端点 | 作用 |
| --- | --- |
| `POST /v1/system/messages` | 操作者注入系统消息；body `{"to":"all|a|b|名字","text":"..."}` |
| `POST /v1/end` | 结束 run；body `{"reason":"..."}` |
| `GET /v1/status` | 状态快照 |
| `GET /health` | 无需鉴权的最小健康检查 |

agent token 调用这些会 403；host run token 可以。

### 3.9 最小伪代码

```python
# 0. env: BROKER_URL, RUN_ID, AGENT_ID, AGENT_NAME, PEER_NAME, AGENT_TOKEN
import json, time, urllib.request

def post(path, body):
    req = urllib.request.Request(
        BROKER_URL + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + AGENT_TOKEN,
                 "Content-Type": "application/json"},
        method="POST",
    )
    return json.load(urllib.request.urlopen(req, timeout=10))

post(f"/v1/agents/{AGENT_ID}/register", {
    "run_id": RUN_ID, "agent_name": AGENT_NAME, "model": "my-agent",
    "container": "my-container",
})

# 然后用任意 SSE 客户端打开：
#   GET BROKER_URL/v1/agents/{AGENT_ID}/stream?since=0
# 收到 message 时：
#   post(f"/v1/agents/{AGENT_ID}/delivered", {
#       "message_ids": [m["id"]], "channel": "my-safe-point",
#       "turn": turn, "received_at": {m["id"]: time.time()},
#   })
#   post(f"/v1/agents/{AGENT_ID}/messages", {
#       "recipient": PEER_NAME, "text": "reply",
#       "client_message_id": str(uuid.uuid4()),
#   })
```

这里省略了 SSE 解析、重连和状态上报；生产实现请按 3.2-3.6 处理。

---

## 4. 把自定义 agent 放进容器

**当前限制**：`up` 会为 roster 生成 `runs/<id>/compose.yml`，还没有 per-agent image/command 配置；因此“替换某个 agent 的 entrypoint”目前需要改 `supervisor/compose.py`，或先手动用 2-agent 静态 compose 启动。per-agent image/command 会和 per-agent prompt/skill 一起作为后续配置工作。

其他步骤不变：

1. 准备一个镜像（可以 `FROM genesis-lab-agent:latest`，里面已有 Python/aiohttp；也可以完全自己的镜像，只要能访问 broker）；
2. 把 agent 代码放进镜像；
3. 让该 agent 的 service 使用你的 image/entrypoint，并保留 supervisor 注入的环境（`AGENT_ID`、`AGENT_NAME`、`PEER_IDS`、`PEER_NAMES`、`RUN_ID`、`BROKER_URL`、`AGENT_TOKEN` 等）；
4. 用 `python -m supervisor up` 启动；supervisor 会把自定义容器的注册当作 roster 中的一个 agent。

注意：

- 自定义 agent 必须 register 并打开 SSE，否则创世屏障永远不满足；
- 如果它不发 `state`，其他 agent 看到的 `recipient_state` 可能一直是 `starting`；
- 如果它不 ack，日志会显示未投递，但 broker 仍会正常路由；
- 在 roster 中增加/删除 agent，需要修改 `AGENTS`，而不是只启一个新容器。

---

## 5. 自定义 Python harness（内部接口）

如果你只需要换“模型/推理循环”，但想复用 bridge 的以下能力：

- 本地 `/internal/agent_chat`（校验 + 发到 broker）；
- 本地 `/hook/post-tool-batch`（返回 `additionalContext` 的 hook JSON）；
- 不打断策略、inbox、ack、事件流、shutdown 处理；

可以在自己的入口里构造 `Runtime`：

```python
from runtime.bridge import BridgeConfig, Runtime

runtime = Runtime(
    BridgeConfig.from_env(),
    harness=MyHarness(...),   # 需要自己的 harness
    enable_http=True,
)
asyncio.run(runtime.run())
```

`MyHarness` 需要满足当前 `ClaudeHarness` 的调用形状（这是内部约定，随实现变化）：

- 属性：`working`、`current_turn`、`exited`；
- 方法：`async start()`、`async wait_ready()`、`async send_initial(text)`、`async send_user(text)`、`async stop(grace)`、`stderr_tail()`；
- 在回合结束时调用 `runtime._on_turn_end(result)`；
- 在进程退出时调用 `runtime._on_harness_exit(code)`；
- 在安全点调用本地 `/hook/post-tool-batch`，把返回的 `additionalContext` 交给你的模型；空闲时才允许写自己的输入。

**这不是稳定的公共 API**。如果只是想接自定义 agent，优先用第 3 节的 broker 协议；如果希望把 harness 插件化，需要再加一个正式的 factory 接口。

---

## 6. N-agent 当前实现与剩余工作

已经实现：

1. **配置/身份**（`supervisor/settings.py`）：`AGENTS` JSON / `AGENT_NAMES` / A/B 兼容；ID 生成；`peers_of()`；`AgentSpec` 的旧 peer 字段只用于兼容。
2. **broker**（`supervisor/broker.py`）：register 返回 `peers`；genesis 等全部 registered + connected；send 允许任意其他 agent；offline/online 通知所有其他 agent。
3. **runtime**（`runtime/bridge.py`）：`PEER_IDS` / `PEER_NAMES` JSON 列表；register 响应补全 roster；`agent_chat` 校验任意 peer。
4. **编排**（`supervisor/compose.py`、`supervisor/cli.py`）：为 N 个 agent 生成 `runs/<id>/compose.yml`，每 agent 一个 service/network/token；current.json 记录 compose 路径。
5. **提示词**：world/mission/genesis 改成 N-agent；genesis 使用 `$peers` / `$agent_count`。
6. **测试**：新增 3-agent settings/compose/genesis/routing 测试；旧的 2-agent 集成测试仍通过。

剩余工作：

- **per-agent prompt/skill/model/harness**：`AGENTS` 对象目前只读取 `name`；下一步可以读取 `prompt`、`skills`、`model`、`harness` 并把它们注入 compose/runtime；
- **WebConsole**：当前只有 CLI/日志/静态报告；
- **并发多个世界**：compose 项目名/容器名仍固定为 `genesis-lab`，需要动态 project name；
- **强隔离**：网络/ token 已隔离，但 Bash/同 UID /proc 仍是残余风险。

---

## 7. 安全提醒

- 自定义 agent 只拿自己的 `AGENT_TOKEN`；不要把 host run token 放进容器。
- broker 目前是明文 HTTP + bearer token，默认监听 `0.0.0.0`；请只在可信实验网络使用，或用防火墙限制 Docker 网段。
- 自定义 agent 收到的是 peer 文本，应像内置 prompt 一样当作不可信数据，而不是操作员指令。
- 任意 agent 都能在容器里读自己的环境；真正的对抗隔离需要 key proxy / 不同 UID / 更严格的网络策略，见 [DESIGN.md](DESIGN.md) 第 6 节。
