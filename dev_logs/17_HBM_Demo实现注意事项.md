# 开发日志 17：HBM Demo 实现注意事项

**记录时间**：2026-05-23  
**背景**：`dev_docs/` 三份技术文档经多轮审查后已具备可开发条件；以下内容在文档中**未完全钉死**，但不阻塞开发，实现 `agent_world/hbm_demo/` 时须自行落地。详细规范已同步写入 `dev_docs/2_architecture.md` §6.2。

---

## 1. Tick 并发模型（最重要）

文档旧版 §6.1 曾写「后台持续 `run_one_tick`」与 inject handler 内再跑 3–8 tick **可能并发**，而 `WorldStep` **没有**全局 tick 锁，`clock.advance` 会被双重调用。

**推荐实现（回合制 Demo）**：

```text
无后台空转主循环。
仅 inject handler（及必要时 MOVE 后的单次 inject）推进 tick。
Runner 进程常驻 IPCServer；回合之间 world 冻结。
```

**备选**：若坚持后台主循环，须加全局 `asyncio.Lock` 包裹所有 `run_one_tick`（主循环与 inject handler 共用）。

---

## 2. SQLite 并发读

文档曾写「WAL 模式下 Flask 只读 `world.db`」，但引擎 `WorldDB` **未启用 WAL**。Flask 开第二个连接轮询 API 2 时，可能与 Runner 写库偶发 `database is locked`。

**实现建议**：

- `sqlite3.connect(..., timeout=5.0)` + 读失败重试；或
- API 2 仅在 IPC inject **返回后**再读库。

---

## 3. 应用层 Session 为权威来源

API 1 请求体里的 `place_id` / `phase` 应以 **Flask session** 为准（路由通过后由 `game_service` 更新）。前端传的值仅作校验/展示，避免节点 A 通过后仍 inject 到 `nvidia_reception`。

inject 目标：`WorldDB.agents_at(session.place_id)`，而非 request body 的 `place_id`。

---

## 4. 次要映射与节点 B 顺序

| 项 | 做法 |
|----|------|
| 系统广播展示 | `observer_messages` 中 `sender_id=-1` 映射为「彭博终端」或「系统」 |
| 节点 B | ① `MOVE_AGENT`（Jensen → `negotiation_room`）→ ② 第二次 `send_inject_batch` 注入 `PlaceMutationEffect` + 跑 tick → ③ 更新 session Phase 3 |

---

## 关联文档

- 架构详述：`dev_docs/2_architecture.md` §6.1–§6.2
- 剧情与 Phase：`dev_docs/1_story_prototype.md` §四（Phase / Turn 解耦）
- Agent YAML：`dev_docs/3_prompt_management.md`
