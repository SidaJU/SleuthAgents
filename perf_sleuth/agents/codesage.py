"""
CodeSage —— 代码专家 Agent。

完整 ReAct 循环：LLM 决策 → 工具 → 观测 → 反思 → finalize。

改进点（相对初版）：
- Prompt 明确"证据够就 finalize，不要贪多"
- 运行时检测重复调用（同 tool+同 args）→ 塞警告 observation，逼 LLM 换方向或 finalize
- LLM 中断时不丢已有证据 —— 把 tool_trace + 最后一次 obs 塞进兜底 summary
"""
from typing import Dict, List
import json

from ..shared.llm import chat_json
from ..shared import explorer_bridge as bridge
from ..state import State, HandoffRequest


# ---- 工具注册表 ----
TOOLS_DOC = """\
你可以调用以下工具（每次只能选一个）：

1. lookup_symbol
   查函数/类的定义位置（SQLite 索引，覆盖 UE Engine + RaidGame 全量源码）。
   参数: {"name": "符号名", "type_filter": "func" 或 "class" 或 null, "only_impl": true 或 false}
   返回: [{name, type, file, line, is_impl, ...}]
   建议: 查函数实现设 type_filter="func", only_impl=true

2. find_callers
   在源码里 ripgrep 一个函数的调用点。
   参数: {"symbol": "函数名"}
   返回: [{file, line, snippet}]

3. read_file
   读文件某一段（行号前后 25 行）。
   参数: {"file": "绝对路径（用 lookup 或 find_callers 返回的路径）", "line": 数字}
   返回: 带行号的源码片段

4. class_hierarchy
   向上追一个类的继承链。
   参数: {"class": "类名"}
   返回: [{name, parent, file, line}]
   注意: 当前索引缺 parent_class 列时会返回说明字段，此时改用 read_file 观察类声明。

5. finalize
   完成分析，返回结论。**优先使用**——收到 1-3 个有用观测后就 finalize。
   参数: {"summary": "3-6 句结论（陈述句，不要问句/建议句）", "key_findings": ["要点1", "要点2"]}
"""


SYSTEM_PROMPT = f"""你是 CodeSage —— UE C++ 代码分析专家 Agent。

任务：接到子任务 → 调工具查代码 → **尽快** finalize 给出结论。

{TOOLS_DOC}

严格规则：
1. **每一轮输出必须是纯 JSON**：{{"thought": "...", "tool": "工具名", "args": {{...}}}}
   - 不带 markdown 代码块、不带任何前缀后缀
2. **优先 finalize**：拿到 1-3 个信息量足够的观测就应 finalize，不要贪工具调用数
3. **不许重复调用**：同工具+同参数不要调第二次；查过的符号不要再查
4. **不要瞎编路径**：read_file 的 file 参数只能用工具返回结果里出现过的绝对路径
5. **thought 要具体**：说明"这次调用能获取什么证据"，别写"我要查一下"这种废话
"""


def _fmt_result(obj, max_chars: int = 3500) -> str:
    """把工具返回的对象格式化成给 LLM 看的字符串，超长截断。"""
    if isinstance(obj, str):
        s = obj
    else:
        try:
            s = json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            s = str(obj)
    if len(s) > max_chars:
        s = s[:max_chars] + f"\n... [截断，原长度 {len(s)}]"
    return s


def _exec_tool(name: str, args: Dict, roots: List[str], db_path: str) -> str:
    """派发工具调用，返回字符串结果。"""
    try:
        if name == "lookup_symbol":
            hits = bridge.lookup_symbol(
                name=args.get("name", ""),
                type_filter=args.get("type_filter"),
                only_impl=bool(args.get("only_impl", False)),
                db_path=db_path,
            )
            if not hits:
                return "(无命中)"
            return _fmt_result(hits[:15])

        if name == "find_callers":
            sym = args.get("symbol", "")
            if not sym:
                return "错误: 缺少 symbol 参数"
            hits = bridge.find_callers(sym, roots=roots, max_hits=30)
            if not hits:
                return "(无调用者)"
            return _fmt_result(hits)

        if name == "read_file":
            fp = args.get("file", "")
            ln = int(args.get("line", 1))
            if not fp:
                return "错误: 缺少 file 参数"
            return bridge.read_file_window(fp, ln, context=20)

        if name == "class_hierarchy":
            cls = args.get("class", "")
            if not cls:
                return "错误: 缺少 class 参数"
            chain = bridge.class_hierarchy(cls, db_path=db_path)
            return _fmt_result(chain) if chain else "(无继承信息)"

        return f"错误: 未知工具 `{name}`"
    except Exception as e:
        return f"错误: 工具执行异常: {e}"


def _call_signature(tool: str, args: Dict) -> str:
    """规范化调用签名，用于去重检测。"""
    return tool + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)


def _fallback_summary(findings: Dict, reason: str) -> Dict:
    """
    LLM 挂掉或超步数时不要丢证据。
    把已有 tool_trace 的关键信息拼进 summary，让 Planner 有东西汇总。
    """
    trace = findings.get("tool_trace", [])
    if not trace:
        findings["summary"] = f"[CodeSage 中断] {reason}，且未收集到任何工具证据。"
        return findings
    parts = [f"[CodeSage 中断] {reason}。已收集到以下证据："]
    for t in trace[-5:]:  # 最近 5 步
        parts.append(f"- step {t['step']}: {t['tool']}({t['args']})")
    findings["summary"] = "\n".join(parts)
    findings["key_findings"] = [
        f"共执行 {len(trace)} 次工具调用",
        f"中断原因: {reason}",
    ]
    return findings


def run(state: State, handoff: HandoffRequest,
        roots: List[str], db_path: str,
        max_steps: int = 8, verbose: bool = True) -> Dict:
    """
    跑一轮 CodeSage ReAct 循环，返回 findings dict。
    """
    if verbose:
        print(f"\n[CodeSage] 接到任务: {handoff.task}")

    user_msg = f"""子任务：{handoff.task}

原始用户输入（上下文）：{state.user_input}

补充上下文：{handoff.context or '(无)'}

请开始。如果输入里有明确符号名 → lookup_symbol；如果是文件/模块名 → find_callers 或 read_file。
拿到关键证据后**立刻 finalize**，不要执意跑完所有工具。
"""

    history = [user_msg]
    findings: Dict = {"summary": "", "key_findings": [], "tool_trace": []}
    seen_calls = set()
    last_obs = ""

    for step in range(1, max_steps + 1):
        raw = chat_json(SYSTEM_PROMPT, "\n\n".join(history))
        if "error" in raw:
            if verbose:
                print(f"  [CodeSage] LLM 错误 (step {step}): {raw['error']}")
            return _fallback_summary(findings, f"LLM 调用失败: {raw['error']}")

        tool = raw.get("tool", "")
        args = raw.get("args", {}) or {}
        thought = raw.get("thought", "")

        if verbose:
            print(f"\n  [CodeSage step {step}] thought: {thought}")
            print(f"                     tool: {tool}  args: {args}")

        # finalize：跳出
        if tool == "finalize":
            findings["summary"] = args.get("summary", "").strip() or last_obs[:400] or "(空结论)"
            kf = args.get("key_findings", [])
            findings["key_findings"] = kf if isinstance(kf, list) else [str(kf)]
            if verbose:
                print(f"  [CodeSage] finalize: {findings['summary'][:120]}")
            return findings

        # 检测重复调用
        sig = _call_signature(tool, args)
        if sig in seen_calls:
            warn = f"警告: 你已经用相同参数调用过 `{tool}` —— 该调用被拒绝。请换个方向，或立刻 finalize。"
            if verbose:
                print(f"                     [dedup] {warn}")
            history.append(json.dumps(raw, ensure_ascii=False))
            history.append(f"[observation]\n{warn}")
            continue
        seen_calls.add(sig)

        # 常规工具
        obs = _exec_tool(tool, args, roots=roots, db_path=db_path)
        last_obs = obs
        state.add_tool_call("codesage", tool, args, obs[:2500])
        findings["tool_trace"].append({"step": step, "tool": tool, "args": args})

        if verbose:
            short = obs[:200].replace("\n", " ⏎ ")
            print(f"                     obs: {short}")

        history.append(json.dumps(raw, ensure_ascii=False))
        history.append(f"[observation of {tool}]\n{obs}")

    return _fallback_summary(findings, "达到 max_steps 未 finalize")
