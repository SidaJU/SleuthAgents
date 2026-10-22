# Agent-1：ReAct —— Agent 到底「是什么」

> 系列定位：这是 Agent 开发的第一块基石。后面所有知识点（工具调用、路由、MCP…）都建立在这个循环之上。

## 0. 一句话

**Agent = 一个会「思考 → 动手 → 看结果 → 再思考」的循环，而不是「问一句答一句」。**

这个循环就叫 **ReAct**（**Re**asoning + **Act**ing，推理 + 行动）。你这个项目里每个 Agent（Planner / CodeSage / AssetSage）的内核，都是同一个 ReAct 循环。

## 1. 先区分：普通问答 vs Agent

- **普通问答**（你之前 `test_lora.py` 那样）：
  ```
  问题 → 模型一次生成 → 答案
  ```
  模型不查任何东西，纯靠自己训练时记住的知识。
- **Agent**：
  ```
  问题 → [思考 → 调用工具拿到真实数据 → 看结果 → 再思考 → ...] → 答案
  ```
  模型可以**动手查外部世界**（代码库、UE 编辑器），而且能**多步推理**（第一步查到 A，第二步根据 A 再查 B）。

> 陌生概念当场解析
> - **Agent（智能体）**：能自主决定「下一步做什么」、并能调用工具影响外部世界的程序。区别于「只会生成文本的模型」。
> - **工具 / Tool**：Agent 能调用的一个函数，比如 `lookup_symbol`（查符号）、连 UE 编辑器的 `get_asset_info`。工具的返回就是「真实世界的数据」。

## 2. ReAct 的四个节拍（核心）

```
┌──────────────────────────────────────────────────┐
│  Thought（思考）：LLM 决定这一步要做什么           │
│        ↓                                          │
│  Act（行动）：LLM 选一个工具 + 参数，执行它         │
│        ↓                                          │
│  Observation（观察）：把工具返回的结果记下来         │
│        ↓                                          │
│  回到 Thought，直到 LLM 说「够了，finalize」        │
└──────────────────────────────────────────────────┘
```

**关键**：每一轮 LLM 都能看到之前所有的「思考 + 观察」，所以它能基于上一步的证据决定下一步。这正是 Agent 比「一次性问答」强的地方。

## 3. 在你代码里的真实样子（CodeSage 的 ReAct 循环）

看 `codesage.py` 的 `run()`，这就是一个标准 ReAct 循环：

```python
def run(state: State, handoff: HandoffRequest, ...):
    history = [user_msg]                      # ← 记忆/草稿本，从用户任务开始
    findings = {"summary": "", "key_findings": [], "tool_trace": []}
    seen_calls = set()

    for step in range(1, max_steps + 1):       # ← 第 2 点说的「循环」
        raw = chat_json(SYSTEM_PROMPT, "\n\n".join(history))   # ← THOUGHT：LLM 决策
        tool = raw.get("tool", "")
        args = raw.get("args", {}) or {}

        if tool == "finalize":                 # ← 停止条件
            findings["summary"] = ...
            return findings

        # ... 去重检查（略） ...
        obs = _exec_tool(tool, args, ...)      # ← ACT：真正执行工具
        history.append(json.dumps(raw, ...))   # ← 把「思考」写进记忆
        history.append(f"[observation of {tool}]\n{obs}")  # ← OBSERVE：把结果写进记忆

    return _fallback_summary(findings, "达到 max_steps 未 finalize")
```

逐行对应第 2 点的四节拍：

| 节拍 | 代码位置 | 发生了什么 |
|------|----------|------------|
| **Thought** | `raw = chat_json(...)` | LLM 返回 `{"thought":"我该查X","tool":"lookup_symbol","args":{...}}` |
| **Act** | `_exec_tool(tool, args)` | 按 LLM 选的工具名和参数去查代码 |
| **Observation** | `history.append("[observation of ...]")` | 把工具结果塞回 `history` |
| **再思考** | 下一轮 `chat_json` 时 `"\n\n".join(history)` | 把全部历史喂回 LLM → 它「看到了」上一步结果，接着推理 |
| **停止** | `tool == "finalize"` | Agent 自己判断证据够了，收尾 |

> 陌生概念当场解析
> - **history（记忆 / 草稿本 / scratchpad）**：一个字符串列表，装着「用户任务 + 每轮的思考 + 每轮的观察」。ReAct 的精髓就在它——LLM 每轮都把整个 history 重新读一遍。没有它，LLM 就成了「金鱼记忆」，每步都忘掉前面查到了什么。
> - **chat_json**：你项目里对 LLM 的封装调用（见 `llm.py`），这里只管「把 history 发给模型、要回一个 JSON 决策」。
> - **max_steps / 兜底（guardrail）**：循环上限（CodeSage 是 8 步）。防止 LLM 陷入死循环永远不 finalize。到了上限就强制用已有证据收尾（`_fallback_summary`）。这是 Agent 工程的「安全带」。

## 4. 嵌套的 ReAct：Agent 之间也有一个循环

你这个项目是**多 Agent** 的，所以 ReAct 有**两层**：

- **内层**：单个 Agent 自己的 `for step` 循环（第 3 点看的 CodeSage）。
- **外层**：`graph.py` 的 `while turn` 循环，在 Agent 之间转手：

```python
for turn in range(planner_max_steps):
    if state.route == "planner":
        planner.run(state, verbose=verbose)
    elif state.route == "codesage":
        ... codesage.run(...) ; state.route = "planner"
    elif state.route == "assetsage":
        ... assetsage.run(...) ; state.route = "planner"
    elif state.route == "done":
        break
```

这里 `state.route`（"下一步跑谁"）就是外层循环的「行动指针」：

```
Planner 跑完 → 把 route 设成 "codesage" → 下一轮跑 CodeSage
CodeSage 跑完 → 把 route 设回 "planner" → Planner 再汇总
```

**外层的「观察」就是读更新后的 `state`。**

> 陌生概念当场解析
> - **state.route（路由指针）**：`state.py` 里的一个字段，决定下一轮谁上场。它是「编排层 ReAct」的 action。
> - **Handoff（交接）**：一个 Agent 把子任务 + 上下文交给另一个 Agent。对应 `state.current_handoff`。

## 5. 串起来：一次完整问答经历几层 ReAct

```
用户输入
  → 外层循环 turn1: Planner(ReAct) 思考 → 决定 handoff_codesage
  → 外层循环 turn2: CodeSage(ReAct 内层 3 步) 查符号→查调用点→finalize
  → 外层循环 turn3: Planner(ReAct) 汇总所有 observation → finalize 给最终答案
```

也就是说：**一次回答 = 外层 3 轮编排 ReAct + 内层若干步工具 ReAct**。这就是 Agent 系统的「形状」。

## 6. 你该记住的 3 件事

1. **ReAct = Thought → Act → Observation → 循环 → finalize**。这是 Agent 区别于普通模型调用的根本。
2. **`history` 是 ReAct 的灵魂**：LLM 每轮重读全部历史，才能多步推理。
3. **`max_steps` 是安全带**：任何 ReAct 循环都必须有上限，否则会死循环。

---

### 下一篇预告

**Agent-2：Tool Calling（工具调用）** —— LLM 怎么「准确」地说出「我要调哪个函数、参数是什么」。会讲 `TOOLS_DOC` 怎么写、JSON 怎么解析（接 `llm.py` 的 `_extract_json`）。
