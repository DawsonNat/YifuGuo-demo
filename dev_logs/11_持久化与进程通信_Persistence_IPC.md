# 开发日志 11：持久化、进程通信与应用层 (Persistence, IPC & App)

**记录时间**：2026-05-22
**目标**：详细记录 `persistence/`、`ipc/` 和 `app/` 目录下的机制。这是连接 Web 前端与后台仿真引擎的桥梁。

---

## 1. 数据持久化：WorldDB (`persistence/world_db.py`)
整个仿真世界的状态和历史记录都保存在一个本地 SQLite 数据库中（通常命名为 `world.db`）。
*   **核心表结构**：
    *   `direct_message`：存储 RDC 私聊和系统广播。包含 `sent_at`, `arrive_at`, `delivered` 字段以支持网络延迟模拟。
    *   `overhear`：存储 F2F 面对面聊天记录。
    *   `group_event`：存储群聊记录。
    *   `script_event_log`：审计日志，记录所有触发的剧本事件和 `UPDATE_STATE` 动作。
*   **作用**：API 2（轮询接口）就是通过只读模式查询这些表，来获取 Tick 流转期间发生的所有对话，并按需分类为 `public_messages` 和 `observer_messages` 返回给前端。

## 2. 进程间通信 (IPC)
由于大模型推理极其耗时，如果直接在 Flask 进程中跑 Tick，会导致 Web 服务被完全阻塞。因此项目采用了基于文件系统的 IPC 机制：
*   **IPCServer (`ipc/server.py`)**：运行在后台的仿真 Runner 进程中。它不断轮询 `ipc_commands/` 目录，接收指令（如 `INJECT_SCRIPT_EVENT`），执行对应的操作（如触发 `WorldStep`），然后将结果写回 `ipc_responses/`。
*   **IPCClient (`ipc/client.py`)**：运行在 Flask Web 进程中。提供 `send_xxx` 方法，将前端的请求转化为 JSON 文件写入 `ipc_commands/`，并等待响应。
*   **CommandType (`ipc/commands.py`)**：定义了支持的指令，其中最核心的是 `INJECT_SCRIPT_EVENT`（用于注入玩家对话和状态改变）。

## 3. Web 应用层 (`app/`)
*   **Flask API (`app/api/simulation.py`)**：提供了对外的 HTTP 接口。
    *   现有的 `POST /simulations/<sim_id>/inject-event` 接口是我们实现 API 1 的基础。它接收 JSON，通过 IPC Client 发送给后台。
    *   **接下来的开发任务**：我们需要改造这个接口（加入异步调用 DeepSeek 生成 `immediate_msg` 的逻辑），并新增一个 `GET /action-result` 接口（用于查询 WorldDB 返回最终结果）。

## 4. 运行器 (`runner/run_agent_world_simulation.py`)
这是后台仿真引擎的主入口脚本。它的启动顺序（Boot Sequence）完美展示了各个模块是如何组装在一起的：
1.  解析配置，初始化 `WorldDB`。
2.  构建 `PlaceStore`, `RelationGraph`, `CapabilityTable`。
3.  构建 `Clock`, `ConnectivityResolver`, `PoolManager`。
4.  构建 `ScriptEngine` 并加载 YAML 剧本。
5.  构建 3 大 Buses、`SegmentStore`、`BehaviorCompressor` 和 `MultiGraphManager` (Zep)。
6.  构建 `WorldState`, `PerceptionBuilder`, `ActionDispatcher`，最终组装出 `WorldStep`。
7.  启动 IPC Server 监听。
8.  进入主循环：不断 `await world_step.run_one_tick()`。