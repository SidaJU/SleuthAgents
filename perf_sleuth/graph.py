"""
编排层 —— Planner ↔ CodeSage ↔ AssetSage 通过 shared state 交接。

拓扑（A 阶段）:

           ┌─────────┐
           │ Planner │
           └────┬────┘
    handoff    │    handoff
       ┌───────┴────────┐
       ▼                ▼
  ┌──────────┐    ┌──────────┐
  │ CodeSage │    │AssetSage │
  └────┬─────┘    └────┬─────┘
       │               │
       └──────┬────────┘
              ▼
        Planner 汇总 → done

用一个 while 循环模拟 LangGraph 的 Command(goto=...)。
Planner 可以多轮 handoff（第一次派 CodeSage 拿到线索后，再派 AssetSage 验证）。
"""
from typing import List
from .state import State
from .agents import planner, codesage, assetsage


def run(user_input: str,
        roots: List[str],
        db_path: str,
        mcp_host: str = "127.0.0.1",
        mcp_port: int = 55557,
        planner_max_steps: int = 6,
        codesage_max_steps: int = 8,
        assetsage_max_steps: int = 6,
        verbose: bool = True) -> State:
    """跑一次完整对话。返回最终 state。"""
    state = State(user_input=user_input)
    state.log("user", user_input)

    for turn in range(planner_max_steps):
        if state.route == "planner":
            if verbose:
                print(f"\n===== turn {turn+1}: Planner =====")
            planner.run(state, verbose=verbose)

        elif state.route == "codesage":
            if verbose:
                print(f"\n===== turn {turn+1}: CodeSage =====")
            handoff = state.current_handoff
            if handoff is None:
                state.error = "route=codesage 但 handoff 为空"
                state.route = "done"
                break
            findings = codesage.run(
                state, handoff,
                roots=roots, db_path=db_path,
                max_steps=codesage_max_steps, verbose=verbose,
            )
            state.findings.append(findings)
            state.log("codesage", findings.get("summary", ""),
                      key_findings=findings.get("key_findings", []))
            state.route = "planner"
            state.current_handoff = None

        elif state.route == "assetsage":
            if verbose:
                print(f"\n===== turn {turn+1}: AssetSage =====")
            handoff = state.current_handoff
            if handoff is None:
                state.error = "route=assetsage 但 handoff 为空"
                state.route = "done"
                break
            findings = assetsage.run(
                state, handoff,
                mcp_host=mcp_host, mcp_port=mcp_port,
                max_steps=assetsage_max_steps, verbose=verbose,
            )
            state.findings.append(findings)
            state.log("assetsage", findings.get("summary", ""),
                      key_findings=findings.get("key_findings", []))
            state.route = "planner"
            state.current_handoff = None

        elif state.route == "done":
            break
        else:
            state.error = f"未知 route: {state.route}"
            break

    if state.route != "done":
        state.error = state.error or "达到 planner_max_steps 循环上限，未 finalize"
        if not state.final_answer:
            state.final_answer = f"[编排层兜底] {state.error}\n\n" + (
                state.findings[-1].get("summary", "") if state.findings else ""
            )

    return state
