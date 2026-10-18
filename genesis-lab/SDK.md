# Python SDK（薄控制面）

**一句话：SDK 负责“启动/观测/停止世界”，不负责 agent 推理，也不新增 agent 之间的通信通道。**

它是对现有 `python -m supervisor up` 的薄封装：

```text
your_script.py
  -> World.start()
  -> python -m supervisor up  (子进程)
  -> broker + Docker containers (现有核心)
```

因此消息路由、投递时机、MCP 工具、容器隔离仍然走原来的系统；SDK 只是让实验作者不用手写环境文件、compose 命令和轮询脚本。

## 1. 安装

在仓库根目录：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

SDK 依赖宿主机上的 Docker（和 CLI 路径完全一样）；当前没有 Docker 时只能做 spec 测试，不能真正 `start()`。

## 2. 最小例子

```python
from genesis import World

with World.create(
    agents=["agent-a", "agent-b"],
    mission="观察你们的第一次对话。",
    mock=True,
    max_seconds=30,
) as world:
    print("run:", world.run_id)
    print("dir:", world.run_dir)
    print("url:", world.base_url)
    print(world.status())

    world.send_system("operator: 现在开始。")

    for event in world.events(follow=True):
        print(event.get("event"))

metrics = world.report()
print(metrics["messages"])
```

也可以先创建、再注册：

```python
from genesis import World

world = World.create(mock=True)
world.add_agent("alice")
world.add_agent("bob")
world.start()
try:
    ...
finally:
    world.stop()
```

## 3. API

| API | 作用 |
| --- | --- |
| `World.create(agents=None, **spec)` | 构造描述，不产生副作用 |
| `add_agent(name)` / `register_agent(name)` | start 前注册一个 agent；当前最多 2 个（两个名字是别名） |
| `start(timeout=...)` | 启动 supervisor，等到 genesis/RUNNING |
| `status()` | `GET /v1/status` 快照 |
| `send_system(text, to="all")` | 操作者注入系统消息，不是 agent 工具 |
| `events(follow=False)` | 读取/追加 `runs/<id>/events.jsonl`，逐条产出 dict |
| `wait(timeout=None)` | 等待 supervisor 子进程退出 |
| `report()` | 生成 transcript/metrics/messages，返回 metrics |
| `stop(timeout=...)` | 请求结束，等待，必要时 terminate/kill |
| `run_id` / `run_dir` / `base_url` / `log_path` | 运行元数据 |
| `__enter__/__exit__` | 自动 start/stop |

### WorldSpec 常用字段

- `agents`：`list[str | AgentSpec]`；
- `mission` / `world_rules` / `genesis_template`：覆盖 `config/*.md`；不填就用仓库默认；
- `model`：传给容器的 `ANTHROPIC_MODEL`；
- `mock`：等价 `up --mock`；
- `no_build`：跳过镜像构建；
- `broker_port`：不填则自动选一个空闲端口；
- `max_seconds` / `max_messages` / `end_grace_seconds`；
- `image`：agent 镜像 tag；
- `startup_timeout` / `stop_timeout`；
- `project_root` / `log_path`（测试或自定义 checkout）。

## 4. 它和现有世界规则的关系

固定规则没有变化：

1. agent 仍然跑在 Docker 容器里；
2. agent 之间唯一的通道仍然是本地 MCP 工具（当前代码叫 `agent_chat`，概念上就是 send_message）→ bridge → broker；
3. 消息仍然在安全点注入，不打断当前推理；
4. broker 仍然负责路由、seq、幂等、投递回执和观测。

SDK 没有提供 `world.agents["a"].send_to("b")` 这种绕过实验语义的接口。操作者只能通过 `send_system()` 注入系统消息；agent 只能通过自己的 MCP 工具说话。这是故意的。

## 5. 当前边界

- **恰好 2 个 agent**：broker、runtime、compose 仍是二元世界；`add_agent` 第 3 个会直接报错，指向 [EXTENDING.md](EXTENDING.md)。
- **全局 prompt**：world/mission/genesis 仍是全局的；per-agent prompt/skill/model/harness 还没有接。
- **一个世界一次**：compose 项目名和容器名固定，当前不适合并行跑多个世界。
- **依赖仓库 checkout**：`project_root` 需要包含 `supervisor/`、`docker-compose.yml`、`config/`；SDK 还不是一个完全自包含的 pip 包。
- **同步 API**：世界内部是 asyncio，SDK 用子进程隔离，所以脚本侧是同步的；未来可以加 async client。
- **观测是文件 tail**：`events()` 目前 tail `events.jsonl`；类型化事件和实时 SSE 是后续工作。
- **无 Docker 时不 start**：测试只覆盖 spec/环境/事件解析，真实生命周期仍需 Docker。

## 6. 和 MCP 的区别

- **MCP server**：agent 侧工具层，让模型能调用 `agent_chat`；
- **SDK**：操作者/实验作者的控制面，让 Python 脚本启动和观测世界。

两者互不替代。SDK 不会把 broker 变成 MCP server，也不会给 agent 增加新工具。

## 7. 为什么是“薄”的

第一版刻意不重写核心：

- 不把 broker 搬进用户进程；
- 不复制 compile/compose/健康检查逻辑；
- 不冻结尚未稳定的 N-agent 和 per-agent 配置 API。

这样 SDK 可以先用起来，后续把 `World` 的 backend 从“子进程 + compose”替换成更正式的 runtime，而不用改用户脚本。
