# Genesis Lab 功能清单

状态：研究实验台（research prototype），不是生产级多 agent 框架。  
当前实验支持 **N >= 2 个 agent**，并带本地 Web Console；per-agent 配置与远程/多用户控制台仍在扩展路线中，见 [EXTENDING.md](EXTENDING.md)。

## 1. 一句话

宿主机上一个 Python supervisor，启动 N 个互相隔离的 Docker 容器；每个容器里一个 Claude Code（默认 DeepSeek `deepseek-flash[1m]`）作为 agent。agent 只能通过 `agent_chat(recipient, message)` 通信，所有消息由 supervisor 路由、排队、记录；收到消息的 agent 不会被中途打断，消息在下一个安全点注入。

## 2. 已实现的能力

### 2.1 世界与编排

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| N >= 2 个独立 Docker 容器 | ✅ | `agent-<id>`；各自独立 network，不共享卷 |
| 创世屏障 | ✅ | 全部 agent 注册并连上 SSE 后，同一时刻推送 genesis |
| 每个容器一个 Claude Code | ✅ | `runtime.bridge` 启动并监护一个 `claude -p --input-format stream-json` |
| 可编辑的 world / mission / genesis | ✅ | `config/world.md`、`mission.md`、`genesis.md`；改完重启，不用重建镜像 |
| 自定义 agent roster | ✅ | `AGENTS` JSON / `AGENT_NAMES`；旧的 `AGENT_A_NAME/B` 仍兼容 |
| 每容器独立 broker token | ✅ | host 持 run token，容器拿 HMAC 派生 token |
| 操作者注入系统消息 | ✅ | `python -m supervisor say` |
| 优雅结束 | ✅ | 最终通知 → shutdown → 收尾 → 停容器；有 grace deadline |

### 2.2 通信与投递

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| `agent_chat(recipient, message)` | ✅ | MCP stdio 工具；recipient 可以是 roster 中任意其他 agent |
| 异步消息 | ✅ | 调用立即返回 message_id / peer_state，不等回复 |
| 不打断当前回合 | ✅ | 正在 working 时绝不写 Claude stdin |
| 工具批次边界注入 | ✅ | `PostToolBatch` hook 的 `additionalContext`，模型下一跳看到 |
| idle 回合边界唤醒 | ✅ | 收到 `result` 后，才用 stdin 开新 user turn |
| 无工具长回合排队 | ✅ | 最坏等到当前回合结束；不做 interrupt |
| 消息顺序 | ✅ | broker 给每个接收方维护递增 seq；SSE 重连可回放 |
| 重试幂等 | ✅ | `client_message_id` 去重聊天消息；`batch_id` 去重 trace |
| 投递回执 | ✅ | `POST /delivered` 记录 channel / turn / latency / network time |
| 消息长度限制 | ✅ | `MAX_MESSAGE_CHARS`（默认 4000） |
| hook 注入预算 | ✅ | `HOOK_BATCH_MAX_CHARS`（默认 8000，低于 Claude Code 10000 上限） |
| 离线/上线通知 | ✅ | 一个 agent 断连/恢复时通知所有其他 agent |
| 消息/发送方校验 | ✅ | 只能发给当前 roster 中的其他 agent；token 只能访问自己的 agent 路径 |

### 2.3 观测与数据

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 事件日志 | ✅ | `runs/<id>/events.jsonl`：消息、投递、状态、turn、usage、生命周期 |
| 对话稿 | ✅ | `transcript.md`：按时间合并所有 agent 消息与系统通知，标注投递渠道/延迟 |
| 指标 | ✅ | `metrics.json`：消息数、channel 分布、延迟分位、每 agent turns/tokens/cost |
| 消息导出 | ✅ | `messages.jsonl`：消息 + 投递 join |
| run 元数据 | ✅ | `run.json`：prompt、配置（密钥脱敏）、限制、镜像、容器侧 broker URL |
| 实时日志 | ✅ | `up` 控制台；`logs --follow` |
| 离线报告 | ✅ | `python -m supervisor report`，可对历史 run 生成/重生成 |

### 2.4 控制与预算

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| CLI | ✅ | init / doctor / up / down / status / say / end / logs / report |
| 时长/消息预算 | ✅ | `RUN_MAX_SECONDS`、`RUN_MAX_MESSAGES` |
| 结束 grace | ✅ | `END_GRACE_SECONDS`，给 agent 最后发言/收尾 |
| 无 key mock | ✅ | `up --mock`：真实 Claude Code + 真实 bridge + 脚本化 Anthropic mock |
| Python 控制面 SDK | ✅ | [genesis/](genesis/) + [SDK.md](SDK.md)：World/WorldSpec/AgentSpec、start/stop/status/events/send_system/report；薄封装现有 supervisor |
| 本地 Web Console | ✅ | `up --web` + [WEBCONSOLE.md](WEBCONSOLE.md)：agent 状态、SSE 事件、系统消息、end、report |
| Docker 自检 | ✅ | 容器/端口/compose/daemon/key/prompt 文件 |
| 启动失败标记 | ✅ | 未到 genesis 就结束的 run 标记为 `startup failed` |

### 2.5 平台与部署

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| Linux 宿主机 | ✅ | Docker Engine + Compose v2；原生 Linux 推荐 |
| Windows 宿主机 | ✅ | Docker Desktop（Linux containers） |
| WSL2 | ✅ | 推荐在 WSL2 内装 Docker Engine；Docker Desktop 场景可用 `BROKER_URL` 覆盖 |
| 容器侧 broker 地址覆盖 | ✅ | `BROKER_URL`；mock 地址会跟随 host |
| N 个 agent 网络隔离 | ✅ | 每个 agent 一个 `net-<id>` |
| 共享文件系统 | ❌（刻意） | 不让 agent 通过文件绕过 supervisor |

### 2.6 质量与安全默认值

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 单元/集成测试 | ✅ | 55 passed, 2 skipped（最近一次） |
| 真实 Claude CLI 集成测试 | ✅ | 2 passed，用 mock LLM，不需要真实 key |
| mock LLM | ✅ | 支持 streaming / 非 streaming、demo / tool_then_end |
| repo hygiene 脚本 | ✅ | 检查 tracked 文件中的 key、私钥、runs、日志 |
| API key 隔离 | ⚠️ | Claude Code 必须在容器内拿 key；agent 理论上可读自己的环境 |
| 强对抗沙箱 | ⚠️ | 有网络隔离和 token 隔离，但 Bash/网络/同 UID /proc 仍是残余风险 |

## 3. 当前不支持的

- **per-agent prompt / skill / model / harness**：当前 N 个 agent 仍共享 world/mission/genesis 和同一模型配置；见第 4 节。
- **生成中途真正注入**：API 语义不支持；只能等工具边界或回合边界。
- **自动重启 / 断点续跑**：bridge/Claude 崩溃后不会 resume。
- **Web Console 的登录 / 多用户 / 远程部署**：控制台无鉴权，默认 loopback；只在 `up --web` 时启动，没有独立的 attach/launch 模式。
- **结构化消息/线程**：目前只有纯文本 + `kind=chat|system`。
- **每消息发送方可见的回执**：发送方只看得到 broker ack 和后续 peer 回复；投递回执进日志。
- **限流 / 成本预算**：只有消息数和时长；没有 token/cost 上限。
- **TLS / 公网鉴权**：broker 默认明文 HTTP + bearer token，建议只在本机/实验网络使用。
- **多机/高可用 broker**：单进程内存 broker。
- **Podman / Kubernetes**：只提供 Docker Compose 路径。

## 4. 当前 agent 数量模型

当前是 **N >= 2 的 agent 世界**：

- roster 由 `AGENTS`（JSON）或 `AGENT_NAMES` 定义；旧的 A/B 配置仍兼容；
- ID 按顺序生成：`a, b, c, ..., z, a1, b1, ...`；
- `supervisor/settings.py` 的 `peers_of()` 返回除自己以外的所有 agent；
- broker 的发送校验允许 roster 中任意其他 agent，genesis 等全部注册并连上；
- `runtime/bridge.py` 的 `PEER_IDS/PEER_NAMES` 是 JSON 列表，register 响应也会返回完整 roster；
- `docker-compose.yml` 只是 2-agent 参考；`up` 会为当前 roster 生成 `runs/<id>/compose.yml`，每个 agent 一个 service/network；
- `config/world.md` / `genesis.md` 已改为 N-agent 文案，genesis 使用 `$peers` / `$agent_count`。

**仍未做**：per-agent prompt / skill / model / harness；目前所有 agent 共享全局 world/mission/genesis 和模型配置。

## 5. 想接自己的 agent？

目前有两条路：

1. **替换其中一个容器里的 runtime**，用任意语言实现 broker 的 HTTP/SSE 协议（推荐，外部接口稳定）。见 [EXTENDING.md](EXTENDING.md) 第 3-4 节。
2. **复用 `runtime.bridge` 的投递机制**，在 Python 里实现一个自定义 harness（内部/实验性接口，不建议长期依赖）。见 [EXTENDING.md](EXTENDING.md) 第 5 节。

## 6. 文件地图

- 入口/编排：[supervisor/cli.py](supervisor/cli.py)、[supervisor/broker.py](supervisor/broker.py)、[supervisor/compose.py](supervisor/compose.py)
- Web Console：[supervisor/webconsole.py](supervisor/webconsole.py)、[WEBCONSOLE.md](WEBCONSOLE.md)
- Python SDK：[genesis/](genesis/)、[SDK.md](SDK.md)、[examples/sdk_world.py](examples/sdk_world.py)
- 容器运行时：[runtime/bridge.py](runtime/bridge.py)、[runtime/harness.py](runtime/harness.py)
- 工具：[runtime/mcp_server.py](runtime/mcp_server.py)
- 投递格式：[runtime/formatting.py](runtime/formatting.py)
- 配置：[supervisor/settings.py](supervisor/settings.py)、[.env.example](.env.example)
- 部署：[docker-compose.yml](docker-compose.yml)、[image/Dockerfile](image/Dockerfile)
- 使用说明：[README.md](README.md)、[DESIGN.md](DESIGN.md)、[EXTENDING.md](EXTENDING.md)

---

如果你需要 N-agent 或自定义 harness 变成第一类能力，按 [EXTENDING.md](EXTENDING.md) 的清单改控制面；这不是改一个常量就能完成的事情。
