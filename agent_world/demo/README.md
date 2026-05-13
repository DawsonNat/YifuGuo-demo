# Agent World 示例 ——《舔狗老公》

一个把内核所有通道都过一遍的中文剧本。

| 内核能力 | 在剧本里体现为 |
|---|---|
| **F2F**（同地点零延迟广播） | 家里：张伟↔思琪；后期咖啡馆：Marcus↔思琪 |
| **RDC**（异地私聊，1 拍延迟） | Marcus → 思琪 "快来开房"；张伟 → 思琪 "想你了"；思琪/安娜互通 |
| **群聊** | "姐妹密谋"群（思琪 + 安娜，group_id=100），实时直播出轨现场 |
| **REQUEST_MOVE** | 思琪从家溜去 boutique_cafe 见 Marcus |
| **B5 四段系统提示词** | 人格内核 / 长期目标 / 当前状态 / 场景行为规则 |
| **场景行为规则**（place.behavior_hint） | 家里："允许说谎、暧昧、甩脸子"；咖啡馆："调情明明白白，想暗示什么直接说" |

## 角色

| ID | 名字 | 设定 |
|---|---|---|
| 1 | 张伟 | 外资银行客户经理，舔狗老公。心里早就察觉，但选择装看不见。 |
| 2 | 思琪 | 前空姐，捞女老婆。和 Marcus 偷情 4 个月。把婚姻当 ATM。 |
| 3 | Marcus | 海归"自由咨询"，知道她有老公，反而觉得刺激安全。 |
| 4 | 安娜 | 思琪闺蜜，表面知心姐姐，私下手机里全是截图存证。 |

## 安装

一次性 editable install（不拉重型 ML 依赖）。装完后 `agent_world` 和 `oasis` 在
任何目录都能 import，不用再 `PYTHONPATH=.`。

```bash
cd /Users/qly/QLY/code/ramus
pip install -e . --no-deps
pip install openai pyyaml pydantic conscribe
```

## API key

`run_demo` 按以下优先级解析 LLM key：

1. `scenario.yaml` 里 `llm.api_key:` 字面量
2. 环境变量 `llm.api_key_env`（默认 `DMXAPI_KEY`）
3. `agent_world/demo/.env` 文件（已 gitignore）

最简单的方式：往 `agent_world/demo/.env` 写一行 `DMXAPI_KEY=sk-...` 就再也不用 export 了。

```bash
echo 'DMXAPI_KEY=sk-d6Kp1XIoc18xLyNShlkNO4du81QavCkyqVxROxEcudJVEhkU' \
    > agent_world/demo/.env
```

## 运行

```bash
# uv 管理的 venv
uv run python -m agent_world.demo.run_demo --num-ticks 6

# 或 plain Python
python3 -m agent_world.demo.run_demo --num-ticks 6
```

可选参数：

* `--config <path>` —— 用别的剧本 YAML
* `--num-ticks N` —— 覆盖 YAML 里的 `num_ticks`
* `--sim-dir <path>` —— 把 `world.db` 留在指定目录（默认 tempdir）
* `--log-level DEBUG|INFO|WARNING` —— 日志详细度

## 输出样例（节选）

```
=================== tick t=0 ===================
  --- agents ---
    [1] 张伟    @home_apartment   state='刚把卡邦尼煮好，蜡烛点上...'
    [2] 思琪    @home_apartment   state='喷的是 Marcus 喜欢的那瓶 Diptyque...'
    [3] Marcus  @boutique_cafe    state='靠墙的卡座坐着，桌上一杯 Negroni...'
    [4] 安娜    @anna_office      state='加班到 9 点，第三杯美式...'
  --- messages this tick ---
    🗣  [F2F]      张伟  ->思琪   ✓ :: 思琪，回家了？我今天买了茅台...
    📨 [RDC]      Marcus->思琪   ✓ :: 思琪，快来了？今晚想不想直接开房？
    👥 [GRP#g100] 安娜  ->思琪   ✓ :: 今晚那位传说中的帅哥又来刷存在感了？
    🗣  [F2F]      思琪  ->张伟   ✓ :: 亲爱的，你辛苦了。我和 Anna 喝一杯就回来。
```

## 文件清单

```
agent_world/demo/
├── __init__.py
├── README.md          (本文)
├── .env               (本地 LLM key，gitignored)
├── .gitignore
├── scenario.yaml      (地点 / 关系 / 群聊 / 角色 / LLM 配置)
├── demo_agent.py      (轻量 DemoAgent: 工具 schema + LLM 调用 + 观察渲染)
└── run_demo.py        (内核装配 + 主循环 + 美化输出)
```

## 想做什么扩展？

* 加更多人物 / 地点 → 改 `scenario.yaml`
* 加新工具（比如 `relation_change`）→ 改 `demo_agent.py::TOOLS`
* 接 ScriptEngine（剧本触发器）→ `run_demo.py` 里把 `script_engine` 从 `None` 换成实例
* 接 Zep 记忆压缩 → `compressor` 同上
* 接 OASIS Platform / FEED actions → 把 `_NullPoolManager` 换成 `MultiPoolPlatformManager`

底层内核 (`agent_world/world/`、`agent_world/buses/` 等) 已经支持上述全部，只是 demo
为了简洁先把它们置空。
