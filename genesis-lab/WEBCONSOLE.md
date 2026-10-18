# Web Console

本地操作员控制台，随 supervisor 一起启动。它不是新的 agent 运行时，也不是远程多租户面板。

## 1. 启动

```bash
python -m supervisor up --web
```

启动后终端会打印类似：

```text
web:     http://127.0.0.1:8788
```

浏览器打开这个地址即可。默认只绑定 loopback；如果需要改端口：

```bash
WEB_CONSOLE_PORT=8899 python -m supervisor up --web
```

也可以写进 `.env`：

```dotenv
WEB_CONSOLE_HOST=127.0.0.1
WEB_CONSOLE_PORT=8788
```

## 2. 能看到什么

- 当前 run id、phase、agent 数量
- 每个 agent 的 state / connected / registered / turn / model / usage / cost
- 实时事件时间线：
  - `message_sent` / `message_delivered`
  - `agent_state` / `agent_registered` / offline/online
  - `genesis` / `run_started` / `run_ending` / `run_ended`
  - `agent_trace` 的 assistant/tool/turn 摘要
- 操作者系统消息（发给 all 或某个 agent）
- 结束 run
- 生成并查看 report（transcript / metrics / messages）

页面的时间线通过 SSE（`/api/events/stream`）实时追加；状态每 1.5 秒轮询一次。

## 3. HTTP API

| 端点 | 方法 | 作用 |
| --- | --- | --- |
| `/` | GET | 单页 UI |
| `/api/status` | GET | broker 状态 + run_dir |
| `/api/events?since=N` | GET | 读取事件，N 是事件序号 |
| `/api/events/stream?since=N` | GET | SSE 实时事件 |
| `/api/say` | POST | `{"text":"...","to":"all"}`，to 也可以是某个 agent 名字 |
| `/api/end` | POST | `{"reason":"..."}` |
| `/api/report` | GET | 写并返回 metrics/transcript |

这些接口没有鉴权；只适合本机实验。

## 4. 安全边界

**不要默认把 `WEB_CONSOLE_HOST` 改成 `0.0.0.0`。**

控制台没有登录，也没有 CSRF token；任何能访问该端口的人都可以：

- 注入系统消息；
- 结束 run；
- 读取 run 目录和事件。

默认 `127.0.0.1` 是刻意的安全边界。如果确实要远程访问，建议 SSH 端口转发，而不是直接暴露：

```bash
ssh -L 8788:127.0.0.1:8788 user@host
```

## 5. 当前限制

- 只能在 `up --web` 时启动；还没有独立的 “attach 到已有 run” 模式，也没有从 UI 启动新世界的表单；
- 一个进程一个 world；compose 项目名固定；
- 没有登录/多用户/权限；
- 事件流是文件 tail，适合本地观察，不是长期审计服务；
- 没有编辑 prompt / 文件 / 容器终端；这些仍通过仓库文件或 CLI；
- 没有 per-agent prompt/skill 配置（见 [EXTENDING.md](EXTENDING.md)）。

## 6. 为什么集成在 supervisor 里

- 控制台和 broker 在同一个 asyncio loop，直接读 `Broker.status()`、调用 `inject_system_message/request_end`，不需要额外代理；
- 生命周期完全绑定 `up`： Ctrl+C、结束、清理容器都不会留下孤立的 web 进程；
- 不需要给浏览器发 host token，也不需要跨进程文件锁。

未来如果要做多用户/远程面板，再把它拆成独立的控制面服务和鉴权层。
