"""
Planner —— 调度专家 Agent（3 Agent 版本）。

职责：
1. 识别输入意图：代码类 / 资产类 / 混合
2. **强制路由**：识别 "用代码专家 / 用资产专家" 短语跳过 LLM
3. LLM 决策：handoff_codesage / handoff_assetsage / finalize
4. 汇总：一个或多个 Agent 返回后综合成最终答案

Planner 是真 ReAct 循环——如果 CodeSage 结果不够可以再 handoff_assetsage 补充。
"""
from typing import Dict
import json

from ..shared.llm import chat_json
from ..state import State, HandoffRequest


# ---- 强制路由关键词 ----
_FORCE_CODE_KW = [
    "用代码专家", "查代码", "看源码", "看代码", "读代码",
    "grep", "ripgrep", "搜代码", "搜源码", "查符号",
    "只用代码", "只查代码", "强制代码",
]

_FORCE_ASSET_KW = [
    "用资产专家", "查资产", "看资产", "查编辑器", "看编辑器",
    "查蓝图", "查材质属性", "查nanite", "查 nanite", "查lod", "查 lod",
    "只用资产", "只查资产", "强制资产",
    "unrealmcp", "unreal mcp",
]


def detect_forced_agent(user_input: str) -> str:
    """规则识别显式路由。返回 'codesage' / 'assetsage' / ''。"""
    low = user_input.lower()
    # 资产关键词优先（更具体）
    for kw in _FORCE_ASSET_KW:
        if kw.lower() in low:
            return "assetsage"
    for kw in _FORCE_CODE_KW:
        if kw.lower() in low:
            return "codesage"
    return ""


PLANNER_TOOLS_DOC = """\
可选动作（一次只选一个）：

1. handoff_codesage
   派给 CodeSage（代码专家，用 ripgrep + SQLite 查 C++ 源码）。
   适用: 函数/类/符号定义、调用点、模块结构、"XXX 在哪里实现"、"YYY 做什么的"
   参数: {"task": "给 CodeSage 的清晰子任务", "context": "补充上下文，可空"}

2. handoff_assetsage
   派给 AssetSage（资产专家，用 UnrealMCP TCP 查 UE 编辑器）。
   适用: 资产 Nanite 是否启用、LOD 数、材质引用、关卡里可见 Actor、组件属性
   参数: {"task": "给 AssetSage 的清晰子任务", "context": "补充上下文，可空"}

3. finalize
   给用户最终答案。
   参数: {"answer": "markdown 格式的最终回答"}
"""


SYSTEM_PROMPT_ROUTE = f"""你是 Planner —— 多 Agent 系统的调度专家。

你面对的输入可能是：
- 自然语言问题（"为什么 xxx 一直发 query"）
- 单个符号名（"UPrimitiveComponent"）
- 文件路径（".../SomeFile.cpp"）
- 模块名（"RDestruction"）
- 资产路径（"/Game/Meshes/SM_Foo"）
- 混合内容

你的任务：判断这个问题**用什么专家最合适**，一次派一个。

{PLANNER_TOOLS_DOC}

判断准则：
- 涉及"源码/函数/类/调用/#include/编译" → codesage
- 涉及"资产属性/Nanite/LOD/材质/关卡 Actor/蓝图默认值/UE 编辑器" → assetsage
- 拿不准时先派 codesage（源码永远可查，UE 编辑器可能没开）
- 用户输入本身就是问候/问工具能力 → finalize

严格规则：
- 输出纯 JSON：{{"thought": "...", "action": "动作名", "args": {{...}}}}
- 每次只选一个动作
"""


SYSTEM_PROMPT_SUMMARIZE = """你是 Planner —— 现在把下游 Agent 的调查结果汇总成最终答案。

输出格式：纯 JSON —— {"thought": "...", "action": "finalize", "args": {"answer": "..."}}

**answer 写作规则（严格遵守）**：
1. 用 markdown 组织
2. 结论必须基于 Agent 返回的原始证据（在 tool_calls 里能查到）
3. 必须引用具体证据：`文件:行号` / 函数名 / 类名 / 资产路径 / MCP 返回字段
4. 禁止："建议进一步分析"、"由于信息不完整"、"需要其他工具"
5. 如果证据里确实没有答案，直接说"根据当前证据发现 X 处相关（列出）但未见明确 Y"
6. 答案长度 100-400 字为宜
"""


def run(state: State, verbose: bool = True) -> str:
    """跑 Planner 单轮决策/汇总。改写 state.route / state.current_handoff / state.final_answer。"""
    # 有 findings → 汇总
    if state.findings:
        return _summarize(state, verbose)

    # 首轮：规则强制路由
    forced = detect_forced_agent(state.user_input)
    if forced:
        state.forced_agent = forced
        state.current_handoff = HandoffRequest(
            to=forced,
            task=f"用户强制指定用{forced}分析：{state.user_input}",
            context=f"用户显式要求走 {forced} 路径，跳过 Planner 自动分析。",
        )
        state.route = forced
        if verbose:
            print(f"[Planner] 检测到强制路由 → {forced}")
        return ""

    # LLM 决策
    return _plan_and_route(state, verbose)


def _plan_and_route(state: State, verbose: bool) -> str:
    user_msg = f"用户输入：\n{state.user_input}\n\n请决定下一步动作。"
    raw = chat_json(SYSTEM_PROMPT_ROUTE, user_msg)
    if "error" in raw:
        state.error = f"Planner LLM 错误: {raw['error']}"
        state.route = "done"
        state.final_answer = f"[Planner 中断] {state.error}"
        return state.final_answer

    action = raw.get("action", "")
    args = raw.get("args", {}) or {}
    thought = raw.get("thought", "")

    if verbose:
        print(f"[Planner] thought: {thought}")
        print(f"[Planner] action: {action}  args: {args}")

    if action == "handoff_codesage":
        state.current_handoff = HandoffRequest(
            to="codesage",
            task=args.get("task", state.user_input),
            context=args.get("context", ""),
        )
        state.route = "codesage"
        return ""

    if action == "handoff_assetsage":
        state.current_handoff = HandoffRequest(
            to="assetsage",
            task=args.get("task", state.user_input),
            context=args.get("context", ""),
        )
        state.route = "assetsage"
        return ""

    if action == "finalize":
        ans = args.get("answer", "").strip()
        state.final_answer = ans or "(Planner 直接 finalize 但未提供 answer)"
        state.route = "done"
        return state.final_answer

    # 未知
    state.error = f"Planner 返回了未知 action: {action}"
    state.route = "done"
    state.final_answer = f"[Planner 中断] {state.error}"
    return state.final_answer


def _summarize(state: State, verbose: bool) -> str:
    """把所有下游 Agent 的证据 + summary 汇总成最终答案。"""
    latest = state.findings[-1] if state.findings else {}

    # 汇集所有 Agent 的 tool_calls 原始观测
    evidence_blocks = []
    for tc in state.tool_calls:
        if tc.agent not in ("codesage", "assetsage"):
            continue
        evidence_blocks.append(
            f"### [{tc.agent}] 工具: {tc.tool}  参数: {json.dumps(tc.args, ensure_ascii=False)}\n"
            f"{tc.result}\n"
        )
    evidence = "\n".join(evidence_blocks) if evidence_blocks else "(无)"

    # 汇集所有 Agent 的 summary
    summaries = []
    for i, f in enumerate(state.findings, 1):
        summaries.append(f"### findings #{i}\n"
                         f"summary: {f.get('summary','(空)')}\n"
                         f"key_findings: {f.get('key_findings', [])}")

    user_msg = f"""原始用户输入：
{state.user_input}

===== 下游 Agent 自己的总结 =====
{chr(10).join(summaries) if summaries else "(无)"}

===== Agent 调用工具收集到的原始证据 =====
{evidence}

===== 你的任务 =====
基于以上原始证据 + 下游总结，写一份最终答案。
证据充分给结论；证据只是列表就诚实说"发现了 X 处，尚需人工判读"。
"""
    raw = chat_json(SYSTEM_PROMPT_SUMMARIZE, user_msg)
    if "error" in raw:
        state.error = f"Planner 汇总时 LLM 错误: {raw['error']}"
        state.route = "done"
        fallback = latest.get("summary", "") or "(无总结)"
        if evidence_blocks:
            fallback += "\n\n**原始证据（LLM 汇总失败，直接展示）**:\n" + evidence[:1500]
        state.final_answer = fallback
        return state.final_answer

    args = raw.get("args", {}) or {}
    ans = args.get("answer", "").strip()
    if not ans:
        ans = latest.get("summary", "(空)")
    state.final_answer = ans
    state.route = "done"
    if verbose:
        print(f"[Planner] 汇总完成 ({len(ans)} chars)")
    return ans
