# 开发日志 08：智能体与记忆管理 (Agent & Memory)

**记录时间**：2026-05-22
**目标**：详细记录 `agent_world/agents/`、`agent_world/memory/` 和 `agent_world/llm/` 目录下的机制，这是 Agent 的大脑和记忆中枢。

---

## 1. 智能体定义 (`agents/`)
*   **OasisAgentProfile (`profile.py`)**：Agent 的静态配置数据类。包含基础信息（名字、ID）、社交数据（粉丝数），以及 Agent World 新增的 6 个核心字段：`location`, `relations`, `capabilities`, `soul`, `long_term_goal`, `current_state`。
*   **Dynamic Tools (`dynamic_tools.py`)**：定义了 Agent 可以调用的核心工具（Tool Calling）：
    *   `speak_to_local`：面对面说话。
    *   `send_message`：发私信。
    *   `request_move`：请求移动到另一个地点。
    *   `update_state`：Agent 主动修改自己的 `current_state`（内心 OS）。

## 2. 大模型抽象层 (`llm/`)
这是一个高度完善且可扩展的 LLM 客户端库：
*   **Providers (`llm/providers/`)**：支持 OpenAI, Anthropic, Google, Ollama 等。
*   **DeepSeek 接入 (`openai_deepseek.py`)**：通过 `ChatDeepSeek` 类继承 OpenAI 兼容接口，支持 Tool Calling 和 Structured Output。**这是我们 Demo 唯一指定的主力模型（DeepSeek-V4-Pro）。**
*   **Schema Optimizer**：自动优化 Pydantic Schema，以适应不同厂商对 Tool Calling 格式的苛刻要求。

## 3. 记忆管理与 Zep 集成 (`memory/`)
为了防止长时间运行导致 Token 爆炸，项目深度集成了 **Zep Cloud** 进行记忆的向量化存储和压缩。
*   **MultiGraphManager (`manager.py`)**：管理多个 Zep Graph（每个 Agent 一个 Graph，每个 Place 一个 Graph）。负责在 Tick 结束时（`flush_all`）将数据异步刷入云端。
*   **BehaviorCompressor (`compressor.py`)**：**行为压缩器**。当 Agent 移动到新地点，或者短期动作积累过多时触发。它会调用 LLM（如 Haiku/Flash 模型）将一堆琐碎的动作压缩成 1-3 句话的摘要，存入 Zep 向量库，并清空短期缓冲区。
*   **SegmentStore (`segment.py`)**：短期记忆缓冲区，存储 Agent 最近发生的 Raw Actions。
*   **Translator (`translator.py`)**：负责将底层的 ActionType（如 `UPDATE_STATE`）翻译成人类可读的自然语言句子，供大模型理解。