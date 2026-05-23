# 开发日志 16：文档查漏补缺与 YAML 配置细节对齐

**记录时间**：2026-05-23
**目标**：在正式编码前，最后一次审查 `dev_docs/`，找出与底层引擎 `scenario.yaml` 规范脱节的细节，并补充到设计文档中。

---

经过对 `agent_world/demo/scenario.yaml` 原有结构的详细比对，发现我们目前的 `3_prompt_management.md` 遗漏了以下 **3 个极其关键的底层配置项**。如果不补充这些，引擎在运行时会直接报错或无法触发核心 Feature：

## 1. 遗漏点：通信覆盖范围 (Coverage & Latency)
*   **问题**：引擎的 RDC（私聊）和 GRP（群聊）是受物理网络限制的。如果不配置 `coverage`，前台无法私聊 Jensen，Jensen 也无法私聊 Tech VP。
*   **补充方案**：必须在 YAML 中显式声明三个地点之间的网络连通性。
    ```yaml
    coverage:
      - {src: nvidia_reception, dst: negotiation_room, latency_ticks: 1}
      - {src: negotiation_room, dst: nvidia_reception, latency_ticks: 1}
      - {src: negotiation_room, dst: tech_vp_office, latency_ticks: 1}
      - {src: tech_vp_office, dst: negotiation_room, latency_ticks: 1}
    ```

## 2. 遗漏点：Agent 通信能力 (Capabilities)
*   **问题**：即使地点连通，Agent 如果没有 `signal_uplink` 能力，依然无法发消息。
*   **补充方案**：必须在 YAML 中赋予所有 6 个 Agent 通信能力。
    ```yaml
    capabilities:
      - {agent_id: 1, capability: signal_uplink}
      # ... agent 2 到 6 同理
    ```

## 3. 遗漏点：人际关系图谱 (Relations)
*   **问题**：引擎的 `phi_rdc` 判定要求两个 Agent 必须是“联系人”才能发私聊（防骚扰机制）。目前我们在文档中没有定义他们之间的关系。
*   **补充方案**：必须在 YAML 中定义他们的职场关系。
    ```yaml
    relations:
      - {src: 1, dst: 2, type: subordinate, symmetric: false} # 前台 -> Jensen
      - {src: 2, dst: 3, type: colleague, symmetric: true}    # Jensen <-> VP
      - {src: 4, dst: 5, type: ally, symmetric: true}         # 海力士 <-> 美光
      - {src: 4, dst: 6, type: ally, symmetric: true}         # 海力士 <-> 三星
    ```

## 4. 遗漏点：群聊配置 (Groups)
*   **问题**：我们在剧情原型中设计了“英伟达高管群”和“存储巨头联盟群”，但没有在 Prompt 管理文档中给出对应的 YAML 结构。
*   **补充方案**：
    ```yaml
    groups:
      - group_id: 100
        name: "NVIDIA 高管群"
        members: [2, 3]
        creator_id: 2
      - group_id: 200
        name: "存储巨头价格联盟"
        members: [4, 5, 6]
        creator_id: 4
    ```

---
**结论**：以上 4 点是底层引擎运行的“硬性依赖”。我将立刻把这些配置补充到 `dev_docs/3_prompt_management.md` 中，使其成为一份可以直接 Copy-Paste 运行的完整 YAML 蓝图。

## 5. 遗漏点：全局仿真配置 (Global Config)
*   **问题**：`scenario.yaml` 必须包含顶层的 `simulation_id`、`clock` 和 `llm` 配置，否则引擎无法启动。
*   **补充方案**：在 `3_prompt_management.md` 顶部补充全局配置块，使其成为一份完整的 YAML 蓝图。
