"""
AssetSage —— 资产专家 Agent。

**任务定位**：查 UE 编辑器里"活着的"资产/组件属性 —— 那些 SQLite 索引和 ripgrep 看不到、
只有编辑器加载后才知道的数据（Nanite 是否真的构建、材质实例的参数值、组件的运行时
配置、蓝图默认值等）。

**完整 Agent**：
- 独立 LLM
- 独立工具集（都通过 UnrealMCP TCP）
- 独立 ReAct 循环
- 独立 finalize / max_steps 兜底
- UE 未启动时优雅降级 —— 每个工具调用都会返回 error dict，LLM 会看到并 finalize

**工具集（B 阶段选最有用的 6 个）**：
1. list_assets         列目录下的资产
2. get_asset_info      读一个资产的元数据（含 Nanite/LOD/材质引用）
3. get_static_mesh_collision_info  静态网格碰撞信息
4. get_available_materials         列可用材质
5. get_visible_static_mesh_actors  当前关卡里可见的 SM Actor
6. scan_actors_for_component        找带某组件的 Actor
"""
from typing import Dict, List, Any
import json

from ..shared.llm import chat_json
from ..shared.mcp_client import UnrealMCPClient
from ..state import State, HandoffRequest


TOOLS_DOC = """\
你可以调用以下 UnrealMCP 工具（每次只能选一个，都通过 TCP 与 UE 编辑器通信）：

1. list_assets
   列出 Content Browser 某目录下的资产。
   参数: {"path": "/Game/xxx", "recursive": true 或 false}
   返回: {status: "success", result: {data: {items: [{name, path, type}]}}}
   注意: 参数名是 **path**（不是 directory）。type = "directory" 表示子目录。

2. get_asset_info
   读一个资产的详细元数据（含 Nanite、LOD、材质、顶点/三角面数、碰撞等 tags）。
   参数: {"asset_path": "/Game/xxx/SM_Foo.SM_Foo"}
   **重要**: asset_path 必须是 ObjectPath 格式，末尾要重复资产名，比如
       "/Game/Meshes/SM_Rock.SM_Rock"
   否则只会返回 Package 信息（class=Package），拿不到 NaniteEnabled/LODs/Materials。
   返回: {tags: {NaniteEnabled, LODs, Materials, Vertices, Triangles, NaniteTriangles, ...}}

3. get_static_mesh_collision_info
   静态网格碰撞详情。
   参数: {"asset_path": "/Game/xxx/SM_Foo"}（此命令用 Package 路径即可）
   返回: {has_body_setup, simple_collision_count, collision_counts: {box/sphere/capsule/convex/...}}

4. get_available_materials
   列出可用材质列表。
   参数: {}

5. get_visible_static_mesh_actors
   当前关卡里可见的静态网格 Actor（含 mesh_path / distance_from_camera / transform）。
   参数: {}
   返回: {actors: [{name, label, class, location, mesh_name, mesh_path, distance_from_camera}]}

6. scan_actors_for_component
   在当前关卡里找带有某类组件的 Actor。
   参数: {"component_class": "StaticMeshComponent"}
   注意: 参数名是 **component_class**（不是 component_class_name）。
   返回: {actors: [{name, class, location, rotation, scale}]}

7. finalize
   完成分析。**拿到 1-3 个有用观测就 finalize，不要贪。**
   参数: {"summary": "3-6 句结论（陈述句）", "key_findings": ["要点1", ...]}
"""


SYSTEM_PROMPT = f"""你是 AssetSage —— UE 编辑器资产/组件专家 Agent。

任务：接到 Planner 的子任务 → 通过 UnrealMCP 查 UE 编辑器里的资产/关卡数据 → 尽快 finalize。

{TOOLS_DOC}

严格规则：
1. **每一轮输出必须是纯 JSON**：{{"thought": "...", "tool": "工具名", "args": {{...}}}}
2. **优先 finalize**：拿到关键证据（比如 Nanite 是否启用、某材质是否引用）立刻停
3. **工具返回错误就停手**：如果连续 2 次工具返回 error（比如 UE 编辑器没开），直接 finalize
   并说明"UnrealMCP 不可达/UE 编辑器未启动，无法查资产"
4. **不许重复调用**：同工具+同参数不要调第二次
5. **thought 要具体**：说清这一步能拿到什么证据
"""


def _fmt(obj: Any, max_chars: int = 3500) -> str:
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


# 白名单：LLM 可能挑到的工具名 → UnrealMCP command_type
_TOOL_MAP = {
    "list_assets": "list_assets",
    "get_asset_info": "get_asset_info",
    "get_static_mesh_collision_info": "get_static_mesh_collision_info",
    "get_available_materials": "get_available_materials",
    "get_visible_static_mesh_actors": "get_visible_static_mesh_actors",
    "scan_actors_for_component": "scan_actors_for_component",
}


def _normalize_args(tool: str, args: Dict) -> Dict:
    """
    参数自动矫正。UnrealMCP 命令的参数名有些反直觉，LLM 容易搞错：
    - list_assets 用 path 不是 directory
    - scan_actors_for_component 用 component_class 不是 component_class_name
    别让 LLM 因为参数名错吃两轮工具调用。
    """
    out = dict(args)
    if tool == "list_assets":
        if "directory" in out and "path" not in out:
            out["path"] = out.pop("directory")
    elif tool == "scan_actors_for_component":
        if "component_class_name" in out and "component_class" not in out:
            out["component_class"] = out.pop("component_class_name")
    return out


def _exec_tool(client: UnrealMCPClient, name: str, args: Dict) -> str:
    """派发工具调用到 UnrealMCP。"""
    if name not in _TOOL_MAP:
        return f"错误: 未知工具 `{name}`"
    cmd = _TOOL_MAP[name]
    fixed_args = _normalize_args(name, args)
    resp = client.call(cmd, fixed_args)
    return _fmt(resp)


def _call_signature(tool: str, args: Dict) -> str:
    return tool + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)


def _fallback_summary(findings: Dict, reason: str) -> Dict:
    trace = findings.get("tool_trace", [])
    if not trace:
        findings["summary"] = f"[AssetSage 中断] {reason}，且未收集到任何工具证据。"
        return findings
    parts = [f"[AssetSage 中断] {reason}。已收集到以下证据："]
    for t in trace[-5:]:
        parts.append(f"- step {t['step']}: {t['tool']}({t['args']})")
    findings["summary"] = "\n".join(parts)
    findings["key_findings"] = [
        f"共执行 {len(trace)} 次 UnrealMCP 调用",
        f"中断原因: {reason}",
    ]
    return findings


def run(state: State, handoff: HandoffRequest,
        mcp_host: str, mcp_port: int,
        max_steps: int = 6, verbose: bool = True) -> Dict:
    """跑 AssetSage ReAct 循环。"""
    if verbose:
        print(f"\n[AssetSage] 接到任务: {handoff.task}")

    client = UnrealMCPClient(host=mcp_host, port=mcp_port)

    # 提前 ping 一次，让 LLM 知道服务是否可用
    ping_err = client.ping()
    preamble = ""
    if ping_err:
        preamble = f"\n\n**警告：UnrealMCP 服务不可达（{ping_err}）** —— UE 编辑器可能未启动或插件未加载。你调用任何工具都会失败，建议直接 finalize 说明情况。"
        if verbose:
            print(f"  [AssetSage] {preamble.strip()}")

    user_msg = f"""子任务：{handoff.task}

原始用户输入（上下文）：{state.user_input}

补充上下文：{handoff.context or '(无)'}
{preamble}

请开始。收到关键证据立刻 finalize。
"""

    history = [user_msg]
    findings: Dict = {"summary": "", "key_findings": [], "tool_trace": []}
    seen_calls = set()
    consecutive_errors = 0

    for step in range(1, max_steps + 1):
        raw = chat_json(SYSTEM_PROMPT, "\n\n".join(history))
        if "error" in raw:
            if verbose:
                print(f"  [AssetSage] LLM 错误 (step {step}): {raw['error']}")
            return _fallback_summary(findings, f"LLM 调用失败: {raw['error']}")

        tool = raw.get("tool", "")
        args = raw.get("args", {}) or {}
        thought = raw.get("thought", "")

        if verbose:
            print(f"\n  [AssetSage step {step}] thought: {thought}")
            print(f"                       tool: {tool}  args: {args}")

        if tool == "finalize":
            findings["summary"] = args.get("summary", "").strip() or "(空结论)"
            kf = args.get("key_findings", [])
            findings["key_findings"] = kf if isinstance(kf, list) else [str(kf)]
            if verbose:
                print(f"  [AssetSage] finalize: {findings['summary'][:120]}")
            return findings

        # 去重
        sig = _call_signature(tool, args)
        if sig in seen_calls:
            warn = f"警告: 相同参数已调用过 `{tool}`，被拒绝。换方向或立刻 finalize。"
            if verbose:
                print(f"                       [dedup] {warn}")
            history.append(json.dumps(raw, ensure_ascii=False))
            history.append(f"[observation]\n{warn}")
            continue
        seen_calls.add(sig)

        obs = _exec_tool(client, tool, args)
        state.add_tool_call("assetsage", tool, args, obs[:2500])
        findings["tool_trace"].append({"step": step, "tool": tool, "args": args})

        # 检测连续错误
        try:
            obs_obj = json.loads(obs)
            if isinstance(obs_obj, dict) and obs_obj.get("status") == "error":
                consecutive_errors += 1
            else:
                consecutive_errors = 0
        except Exception:
            consecutive_errors = 0

        if verbose:
            short = obs[:200].replace("\n", " ⏎ ")
            print(f"                       obs: {short}")

        if consecutive_errors >= 2:
            if verbose:
                print("  [AssetSage] 连续 2 次错误，强制 finalize")
            return _fallback_summary(
                findings,
                f"UnrealMCP 连续 {consecutive_errors} 次返回错误，判定服务不可用"
            )

        history.append(json.dumps(raw, ensure_ascii=False))
        history.append(f"[observation of {tool}]\n{obs}")

    return _fallback_summary(findings, "达到 max_steps 未 finalize")
