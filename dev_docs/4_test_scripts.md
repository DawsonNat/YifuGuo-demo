# 4. 测试与验证脚本 (Test Scripts)

**文档目标**：提供在 Web 前端 UI 开发完成之前，后端能够独立闭环测试“双段式异步 API”和“Tick 流转”的 Python 测试脚本。

**架构参照**：严格遵循 `dev_logs/11_持久化与进程通信_Persistence_IPC.md` 中定义的 API 接口规范，以及 `dev_logs/10_剧本引擎与事件注入_Script_Engine.md` 中定义的 `DialogueInjectionEffect` 注入格式。

---

## 一、 测试环境准备

在运行测试脚本之前，需要确保后端引擎的两个进程都在运行：

1.  **启动 IPC Server 与仿真引擎 (Runner 进程)**：
    ```bash
    # 假设我们已经创建了 jensen_scenario.yaml
    python3 -m agent_world.runner.run_agent_world_simulation \
        --config agent_world/demo/jensen_scenario.yaml \
        --sim-dir /tmp/jensen_sim
    ```
2.  **启动 Flask Web 服务 (API 进程)**：
    ```bash
    # 假设 Flask app 运行在 5000 端口
    FLASK_APP=agent_world.app:create_app flask run --port 5000
    ```

---

## 二、 自动化测试脚本 (`test_async_api.py`)

该脚本模拟了前端的行为：发送玩家 Query -> 验证极速响应 -> 轮询等待后台 Tick 跑完 -> 打印最终的上帝视角和主聊天框数据。

```python
import time
import requests
import uuid

# 配置
BASE_URL = "http://127.0.0.1:5000/api/simulation/shedog_husband"
JENSEN_AGENT_ID = 2

def test_interaction():
    print("=== 开始测试双段式异步 API ===")
    
    # 1. 构造玩家输入的 Script Event (DialogueInjectionEffect)
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    payload = {
        "event": {
            "id": task_id,
            "trigger": {
                "type": "at_time",
                "t": 0  # 立即触发
            },
            "effect": {
                "type": "dialogue_injection",
                "agent_id": JENSEN_AGENT_ID,
                "text": "玩家说：黄总，我的底层算法能让渲染速度提升 10 倍。"
            }
        }
    }

    # 2. 调用 API 1：发起交互
    print(f"\n[1] 发送 POST 请求注入事件 (Task ID: {task_id})...")
    start_time = time.time()
    res1 = requests.post(f"{BASE_URL}/inject-event", json=payload)
    res1_data = res1.json()
    elapsed = time.time() - start_time
    
    assert res1.status_code == 200
    assert res1_data["success"] is True
    
    immediate_msg = res1_data["data"].get("immediate_msg")
    print(f"✅ 成功获取 API 1 即时响应 (耗时: {elapsed:.2f}s)")
    print(f"   -> 极速动作描写: {immediate_msg}")
    
    # 3. 调用 API 2：轮询结果
    print("\n[2] 开始轮询 API 2 获取最终结果...")
    max_retries = 15
    poll_interval = 2.0
    
    for i in range(max_retries):
        res2 = requests.get(f"{BASE_URL}/action-result?task_id={task_id}")
        res2_data = res2.json()
        
        status = res2_data["data"].get("status")
        if status == "processing":
            print(f"   - 轮询 {i+1}/{max_retries}: 后台 Tick 正在流转，等待中...")
            time.sleep(poll_interval)
            continue
            
        elif status == "completed":
            print(f"\n✅ 成功获取 API 2 最终结果 (后台 Tick 流转完毕)!")
            data = res2_data["data"]
            
            print("\n--- 👁️ 上帝视角 (Observer Messages) ---")
            for msg in data.get("observer_messages", []):
                print(f"[Tick {msg['tick']}] {msg['sender']} -> {msg['receiver']} ({msg['type']}): {msg['content']}")
                
            print("\n--- 💬 主聊天框 (Public Messages) ---")
            for msg in data.get("public_messages", []):
                print(f"[{msg['sender']}] ({msg['type']}): {msg['content']}")
                
            return
            
    print("\n❌ 轮询超时：后台引擎未能在规定时间内完成 Tick 流转。")

if __name__ == "__main__":
    test_interaction()
```

---

## 三、 预期测试结果分析

运行上述脚本后，如果底层引擎和 API 开发正确，控制台应输出类似以下内容：

1.  **API 1 阶段**：耗时极短（< 1秒），打印出 DeepSeek-V4-Pro 极速生成的 `immediate_msg`（如：“Jensen 微微皱眉，手指敲击着桌面...”）。
2.  **轮询阶段**：打印 2-3 次“等待中...”，这期间后台的 `WorldStep` 正在跑 Tick，Jensen 正在调用主模型思考并私聊 Tech VP。
3.  **最终结果阶段**：
    *   **上帝视角**会打印出 Jensen 和 Tech VP 之间的 RDC 私聊记录（验证了核心 Feature 成功触发）。
    *   **主聊天框**会打印出 Jensen 最终对玩家说的话。

只要这个测试脚本能跑通，就证明我们的后端架构已经完美实现了“掩盖大模型延迟”和“展示 Multi-Agent 内部交互”的两大核心商业目标。
