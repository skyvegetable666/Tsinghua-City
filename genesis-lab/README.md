# Genesis Lab

> 两个 Claude Code agent，两个隔离的 Docker 容器，一个 Python supervisor。
> agent 之间唯一的通道是 `agent_chat(recipient, message)`；消息全部由 Python 系统路由。
> 收到消息的 agent **不会被打断当前推理**，消息会在最早的安全点注入。

这是一个可以直接跑的实验台。宿主机（Windows）上只有一个 Python 程序，它负责：

- 启动两个 Docker 容器；
- 等待双方就绪，在同一时刻发送 **创世纪（genesis）**；
- 作为消息系统，路由、排队、记账 `agent_chat` 消息；
- 记录完整事件流（JSONL）、对话稿（transcript）和指标（metrics）；
- 支持操作者在运行中注入系统消息，或优雅结束实验。

**没有 API key 也能先跑通全链路**：`python -m supervisor up --mock` 会用脚本化的假模型替代 DeepSeek。

---

## 1. 它到底怎么跑

```
Windows host
┌────────────────────────────────────────────────────────────────────┐
│  python -m supervisor up                                           │
│    ├─ Supervisor broker  (aiohttp, 0.0.0.0:8787)                    │
│    │    ├─ /v1/agents/{a,b}/register, /stream (SSE), /state        │
│    │    ├─ /v1/agents/{a,b}/messages   ← agent_chat 的出口          │
│    │    ├─ /v1/agents/{a,b}/delivered  ← 真正注入后的回执            │
│    │    └─ runs/<run-id>/events.jsonl, transcript.md, metrics.json │
│    ├─ Docker Engine                                                 │
│    │    ├─ agent-a  (仅 net-a)                                      │
│    │    │    bridge.py  ──  claude -p --input-format stream-json    │
│    │    │       │              │                                    │
│    │    │       │              ├─ MCP stdio: agent_chat             │
│    │    │       │              └─ PostToolBatch hook (本地 HTTP)     │
│    │    └─ agent-b  (仅 net-b)   同样结构                            │
│    └─ 可选：Mock LLM (0.0.0.0:8790)，给 --mock 用                  │
└────────────────────────────────────────────────────────────────────┘
```

一次完整生命周期：

1. `supervisor up` 先启动 broker，再 `docker compose up -d`。
2. 两个容器里的 `runtime.bridge` 分别注册，并建立 SSE 长连接。
3. **双方都连上**后，broker 在同一微秒级时刻推 `genesis` 事件；两边的 bridge 各自启动一个 Claude Code 进程，把 genesis prompt 作为第一条 user message 写入 stdin。
4. agent 调 `agent_chat(recipient="agent-b", message="...")`：MCP 进程 → 容器内 bridge → 宿主机 broker。
5. broker 分配 `message_id` 与接收方序号，写入事件日志，然后经 SSE 推给接收方 bridge。
6. 接收方按“安全点策略”注入（见下节）。
7. `supervisor end`（或预算/时长到期）→ 系统通知 → `shutdown` 事件 → agent 收尾 → 容器退出 → 生成报告。

---

## 2. 为什么“不打断推理”，以及消息什么时候到

模型的单次 API 请求一旦发出，就无法在生成中途向它的上下文里插入 token。因此“真正的中途提醒”只有两条路：

- 取消当前请求（`interrupt`）——会打断推理、丢掉当前回合；
- 等下一个安全点。

这个实验选择 **只等安全点，不打断**。安全点有两个：

| 安全点 | 触发 | 注入方式 | 延迟 |
| --- | --- | --- | --- |
| 工具批次结束 | 每个 `PostToolBatch` hook | `additionalContext` 附在工具结果旁，模型下一跳看到 | 当前工具批次结束后、下一次模型请求前 |
| 回合结束（idle） | 收到 Claude Code 的 `result` 事件 | bridge 在**没有进行中的回合**时写 stdin，新开一个 user turn | 当前回合结束后立刻 |
| 生成过程中 | 无工具调用 | **排队等待** | 最坏 = 当前回合剩余时间 |

硬不变量（代码里由状态机保证）：

- **bridge 只在 `state == idle` 时写 Claude 的 stdin**；正在跑回合时，stdin 一行都不写。
- 回合中的投递只通过 Claude Code hook，hook 是同步调用，会在下一次模型请求前返回；它不会取消当前回合。
- 因此，agent 收到消息的语义是“在下一个可插入的位置看到它”，而不是“立刻被打断”。

> 为什么 hook 用 `PostToolBatch` 而不是 `PreToolUse` / `Stop`：
> `PostToolBatch` 每个工具批次只触发一次，且正好在“工具结果已产生、下一次模型请求尚未发出”的位置；它同时覆盖 MCP 工具调用。相比之下 `PostToolUse` 在并行工具时触发多次，`Stop` 只在回合结束触发。Claude Code 会把 `additionalContext` 包成 system reminder，插在 hook 触发点的上下文里。

消息被注入后，bridge 会向 broker 发 `delivered` 回执，记录：

- `channel`：`hook` 或 `turn`；
- `turn`：注入发生在第几个回合；
- `latency_s`：从 broker 收到消息到注入完成；
- `network_s`：从 broker 收到消息到 bridge 收到 SSE 的排队/网络时间。

这让实验可以区分“网络延迟”和“等待安全点的延迟”。

---

## 3. 隔离：两个容器互相看不见

- `agent-a` 只接入 `net-a`，`agent-b` 只接入 `net-b`。它们不共享 Docker network，DNS 上也发现不了对方。
- 不挂载共享卷。两个容器之间没有文件系统通道。
- 唯一的出口是 `host.docker.internal:<broker-port>` 的 broker；broker 强制 `recipient` 只能是自己的 peer。
- 每个容器拿到的是**独立派生**的 broker token（HMAC(run_token, agent_id)），host 持有 run token。一个容器无法伪装成另一个容器调用 API。
- 容器内的 bridge 只是 Claude Code 的“进程监护 + 本地 hook/MCP 端点”，不做消息路由；所有路由都在宿主机 Python。

---

## 4. 目录结构

```
genesis-lab/
├── supervisor/              # 宿主机 Python：system / broker / CLI
│   ├── broker.py            #   唯一消息路由、创世纪、presence、结束
│   ├── cli.py               #   up / down / say / end / status / logs / report
│   ├── report.py            #   events.jsonl → transcript.md / metrics.json
│   ├── mock_llm.py          #   无 key 的 Anthropic 兼容假模型
│   ├── settings.py          #   .env、身份、run token、per-agent token
│   ├── dockerctl.py         #   docker compose 包装
│   └── store.py             #   JSONL 事件日志
├── runtime/                 # 容器内 Python：agent 运行时
│   ├── bridge.py            #   状态机 + 投递策略 + 本地 HTTP
│   ├── harness.py           #   Claude Code 子进程 / stream-json / hook 配置
│   ├── broker_client.py     #   HTTP + SSE 客户端
│   ├── inbox.py             #   有序、去重、按预算取消息
│   ├── formatting.py        #   注入文本格式
│   ├── mcp_server.py        #   唯一工具 agent_chat
│   └── hooks/deliver.py     #   PostToolBatch hook → 本地 bridge
├── config/
│   ├── world.md             #   静态世界规则，作为 append-system-prompt
│   ├── mission.md           #   本次实验的任务，可改
│   └── genesis.md           #   创世纪模板，可改
├── image/Dockerfile         #   node + python + claude-code 固定版本
├── docker-compose.yml       #   两个容器、两个隔离网络
├── tests/                   #   单测 + 真实 Claude CLI 集成测试
├── .env.example
├── README.md
└── DESIGN.md                #   更细的协议、失败语义、备选方案
```

---

## 5. Windows 快速开始

### 前置条件

- Windows 10/11 + **Docker Desktop**（Linux containers 模式）；
- Python 3.11+（宿主机只需跑 supervisor）；
- 第一次构建镜像需要网络（npm/pip/Docker Hub）。

### 5.1 安装宿主机依赖

```powershell
cd genesis-lab
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r supervisor\requirements.txt
```

### 5.2 生成配置

```powershell
python -m supervisor init
```

这会从 `.env.example` 复制出 `.env`。现在先不用填 key。

### 5.3 无 key 干跑（强烈建议先做）

```powershell
python -m supervisor up --mock
```

- 首次会 `docker compose build`，通常 2–5 分钟；
- 两个容器里是真实的 Claude Code，只是 API 指向宿主机的脚本化 mock；
- 终端会显示注册、创世、消息投递日志；
- `Ctrl+C` 结束（会给 agent 发收尾通知后停容器）；
- 结束后看 `runs\<run-id>\transcript.md`。

> `--mock` 只验证管道。mock 的回复是固定脚本，不代表模型行为。

### 5.4 填 API key，跑真实模型

编辑 `.env`：

```dotenv
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_AUTH_TOKEN=sk-你的-key
ANTHROPIC_MODEL=deepseek-flash[1m]
```

（默认值已按 DeepSeek 的 Claude Code 接入文档配置；`[1m]` 是 Claude Code 的上下文窗口提示后缀，实际请求的模型仍是 `deepseek-flash`。如果 key 来自别的网关，只改 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` 即可。）

检查环境：

```powershell
python -m supervisor doctor
python -m supervisor up
```

运行中可以在另一个 PowerShell 里：

```powershell
.venv\Scripts\Activate.ps1
python -m supervisor status
python -m supervisor say "operator: 请注意你们只有十分钟。"
python -m supervisor say --to a "只对你说的系统消息"
python -m supervisor logs --follow
python -m supervisor end --reason "时间到"
```

> `doctor` 会检查 Docker、compose、端口、prompt 文件和 key。没有 key 时用 `doctor --mock`。

### 5.5 改实验内容

- `config\mission.md`：本次任务，插入 genesis；
- `config\world.md`：世界规则，作为 Claude Code 的 system prompt 附加；
- `config\genesis.md`：创世纪模板，可用占位符 `$agent_name`、`$peer_name`、`$mission`、`$run_id`、`$genesis_time`。

改完重启 `up` 即可（容器镜像不用重建）。

---

## 6. 命令一览

| 命令 | 作用 |
| --- | --- |
| `python -m supervisor init` | 从 example 生成 `.env` |
| `python -m supervisor doctor [--mock]` | 检查 Python/Docker/compose/端口/key |
| `python -m supervisor up [--mock] [--no-build] [--max-seconds N] [--max-messages N]` | 前台运行整个世界 |
| `python -m supervisor status` | 打印 broker 状态 JSON |
| `python -m supervisor say TEXT [--to all\|a\|b\|名字]` | 注入系统消息 |
| `python -m supervisor end [--reason ...]` | 优雅结束 |
| `python -m supervisor down` | 强制停止容器 |
| `python -m supervisor logs [--follow] [--run-dir ...]` | 看事件流 |
| `python -m supervisor report [--run-dir ...]` | 生成/打印 transcript 与 metrics |

`up` 是**前台进程**，broker 就在这个进程里。正常 Ctrl+C / 退出 `up` 会结束会话并停止容器；如果进程被强杀，可能留下容器，用 `python -m supervisor down` 清理。

---

## 7. 配置项（.env）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ANTHROPIC_BASE_URL` | DeepSeek Anthropic 端点 | Claude Code 的 API 网关 |
| `ANTHROPIC_AUTH_TOKEN` | 空 | API key；没有就 `--mock` |
| `ANTHROPIC_MODEL` | `deepseek-flash[1m]` | 主模型 |
| `AGENT_A_NAME` / `AGENT_B_NAME` | `agent-a` / `agent-b` | 双方名字，也是 `agent_chat` 的 recipient |
| `MAX_MESSAGE_CHARS` | 4000 | 单条消息上限 |
| `HOOK_BATCH_MAX_CHARS` | 8000 | 单次 hook 注入预算（Claude Code 上限 10000） |
| `RUN_MAX_MESSAGES` | 0 | 聊天消息数上限，0 = 不限 |
| `RUN_MAX_SECONDS` | 0 | 时长上限，0 = 不限 |
| `END_GRACE_SECONDS` | 45 | 收到结束指令后给 agent 收尾的时间 |
| `AGENT_IMAGE` | `genesis-lab-agent:latest` | 镜像 tag |
| `BROKER_PORT` | 8787 | 宿主机 broker 端口；容器通过 `host.docker.internal` 访问 |
| `MOCK_LLM_PORT` | 8790 | mock 模式端口 |
| `BROKER_TOKEN` | 空 | 留空则每次运行随机生成（推荐） |
| `WORLD_FILE` / `MISSION_FILE` / `GENESIS_FILE` | config/... | prompt 文件路径 |

---

## 8. 观测与数据

每次 `up` 在 `runs\<UTC 时间戳>-xxxx\` 下产生：

- `events.jsonl`：一行一个事件，带 `ts` / `ts_iso` / `run_id`。主要事件：
  - `run_started`、`agent_registered`、`genesis`（含完整 prompt）；
  - `message_sent`（含消息全文、发送方、接收方、id、seq）；
  - `message_delivered`（`channel`、`turn`、`latency_s`、`network_s`）；
  - `agent_state`、`agent_trace`（assistant 文本、tool_use 名称/参数预览、每回合 usage/cost）；
  - `run_ending`、`shutdown_sent`、`run_ended`。
- `messages.jsonl`：消息 + 投递记录（report 时生成）；
- `transcript.md`：按时间排列的双方对话，标注投递渠道与延迟；
- `metrics.json`：消息数、投递渠道分布、延迟分位数、每 agent 回合/token/成本；
- `run.json`：本次实验的 prompt、配置（密钥已脱敏）、限制、镜像。

分析示例（Python）：

```python
import json
events = [json.loads(line) for line in open("runs/<run>/events.jsonl", encoding="utf-8")]
deliveries = [e for e in events if e["event"] == "message_delivered"]
print(sum(e["latency_s"] for e in deliveries) / len(deliveries))
```

---

## 9. 故障排查

| 现象 | 排查 |
| --- | --- |
| `docker compose` 找不到 | Docker Desktop 未启动 / CLI 不在 PATH；`python -m supervisor doctor` |
| 容器起不来 | `docker compose logs --tail 100 agent-a`；常见是镜像构建失败或 key 为空 |
| bridge 一直 “waiting for genesis” | 另一个 agent 没连上；看 `logs`；`/health` 看 phase 和 agents |
| 端口 8787 被占用 | 改 `BROKER_PORT`；或 `down` 旧 run |
| 容器访问不到 broker | Docker Desktop 的 `host.docker.internal`；Windows 防火墙允许 Python；`BROKER_HOST` 必须是 `0.0.0.0`。如果仍不通，删掉 compose 里的 `extra_hosts`，让 Docker Desktop 自己解析该名字 |
| 真实模型报鉴权/模型错误 | 检查 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`；先 `--mock` 排除管道问题 |
| 出现 `unrecognized_model` 警告 | DeepSeek 模型名不在 Claude Code 模型目录里，属预期；`CLAUDE_CODE_MAX_CONTEXT_TOKENS` 已设置 |
| 消息很晚才到 | 正常：如果对方在长回合里且没有工具调用，最快也要等回合结束；见第 2 节 |
| 两个 agent 都不说话 | 看 transcript 里 genesis 是否到达；检查 mission 是否为空；可 `say` 推一把 |

日志位置：

- 宿主机：`runs\<id>\events.jsonl`；
- 容器 stdout：`docker compose logs -f agent-a`（bridge + claude 的 stderr 都在里面）。

---

## 10. 已知限制（重要）

1. **不存在真正的“生成中途注入”**。无工具长回合里的消息只能等回合结束；这是 API 语义决定的。本设计保证“不打断”，代价是最坏延迟=当前回合剩余时间。
2. **API key 在容器内可见**。Claude Code 必须拿到 key；有 Bash/Read 的 agent 理论上能读到自己的环境或同 UID 的进程。建议用**专用、限额的 key**，不要把生产 key 放进来。更严格的对抗场景需要 key 代理 + 不同 UID 运行 bridge。
3. **agent 有 Bash 和网络**。broker token 已从 Claude 子进程环境里移除，容器间也网络隔离；但这不是针对恶意 agent 的强沙箱。若要严格禁止外部通道：去掉 Bash/网络工具，或加 egress proxy。
4. **没有自动重启/断点续跑**。一个 bridge/Claude 挂了，本次运行继续等另一个；peer 会收到 offline 系统消息。研究实验建议一次 run 一次分析。
5. **broker 在内存里**。`up` 进程退出后无法恢复该 run（但 JSONL 已落盘）。
6. 消息默认上限 4000 字符；hook 单次注入预算 8000 字符（Claude Code 的 additionalContext 上限是 10000）。
7. 单 agent 单次只连一个 peer；recipient 只允许自己的 peer。

---

## 11. 测试与验证

宿主机单元/集成测试：

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

覆盖：消息去重/排序/预算、broker 创世屏障、路由与投递回执、per-agent token 隔离、非打断投递策略（working 时绝不写 stdin）、idle 唤醒、回合结束唤醒、系统消息、结束流程。

真实 Claude Code 集成测试（需要宿主机有 `claude` CLI；仍然不需要 API key，用 mock LLM）：

```powershell
$env:GENESIS_RUN_CLI_TESTS="1"
python -m pytest -q tests/test_cli_integration.py tests/test_two_agent_world.py -s
```

这两个测试会启动真实的 broker、真实的 `runtime.bridge` 子进程、真实的 Claude Code CLI 和脚本化 mock 模型，验证：

- 正在跑回合时消息通过 `PostToolBatch` 进入下一个模型请求；
- idle 时消息通过 stdin 开启新回合；
- 真实的 `mcp__agentchat__agent_chat` 工具调用能穿过 MCP → bridge → broker；
- 两个 agent 能互相收发并优雅结束。

> 这些测试在 Linux/macOS 上开发验证；Windows 下依赖 Docker Desktop 的容器路径未被本仓库的测试覆盖，请用 `python -m supervisor up --mock` 做首次端到端确认。

---

## 12. 设计文档

协议细节、失败语义、为什么不用 interrupt / 共享卷 / 直接容器互联，见 [DESIGN.md](DESIGN.md)。

---

## 13. 快速检查清单

- [ ] Docker Desktop 启动，`docker info` 正常
- [ ] `python -m supervisor doctor` 通过
- [ ] `up --mock` 成功，transcript 里有两条问候
- [ ] 填 key，`up` 成功
- [ ] 用 `say` / `end` / `report` 观察一次完整 run
- [ ] 调整 `config/mission.md`，开始正式实验
