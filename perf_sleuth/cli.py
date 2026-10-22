"""
CLI 入口。

用法:
    python -m perf_sleuth.cli "问题描述"
    python -m perf_sleuth.cli --verbose "问题描述"
    python -m perf_sleuth.cli --no-verbose "问题描述"
"""
import argparse, os, sys, json

# tomllib in stdlib since 3.11; fallback to tomli
try:
    import tomllib as _toml
except ImportError:
    try:
        import tomli as _toml
    except ImportError:
        _toml = None

from .graph import run as run_graph
from .shared.explorer_bridge import resolve_db_path
from .shared.llm import probe


_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.normpath(os.path.join(_HERE, "..", "config.toml"))


def load_config(path: str) -> dict:
    """加载 TOML；找不到时用默认值。"""
    if not os.path.exists(path):
        print(f"[warn] 配置文件不存在: {path}，使用默认")
        return {
            "index": {"db_path": ""},
            "roots": {"paths": []},
            "limits": {"planner_max_steps": 6, "codesage_max_steps": 8},
        }
    if _toml is None:
        print("[warn] 未安装 tomli / tomllib，无法解析 config.toml，使用默认")
        return {"index": {"db_path": ""}, "roots": {"paths": []},
                "limits": {"planner_max_steps": 6, "codesage_max_steps": 8}}
    with open(path, "rb") as f:
        return _toml.load(f)


def main():
    ap = argparse.ArgumentParser(prog="perf_sleuth",
                                 description="UE 项目多 Agent 分析（A 阶段：Planner + CodeSage + AssetSage）")
    ap.add_argument("query", nargs="+", help="用户问题 / 符号名 / 文件路径 / 模块名 / 资产路径")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--verbose", action="store_true", default=True)
    ap.add_argument("--no-verbose", action="store_false", dest="verbose")
    ap.add_argument("--dump-trace", metavar="FILE",
                    help="把完整 tool_calls / messages 写到 JSON 文件")
    args = ap.parse_args()

    user_input = " ".join(args.query)

    cfg = load_config(args.config)
    roots = cfg.get("roots", {}).get("paths", []) or []
    roots = [r for r in roots if r and os.path.isdir(r)]
    if not roots:
        print("[warn] 配置里没有可用的 roots，find_callers 将无处可搜")

    try:
        db_path = resolve_db_path(cfg.get("index", {}).get("db_path", "") or "")
    except FileNotFoundError as e:
        print(f"[error] {e}")
        sys.exit(2)

    err = probe()
    if err:
        print(f"[error] {err}")
        print("        请先 `ollama serve` 或设置 UE_EXPLORER_LLM_BASE")
        sys.exit(3)

    mcp_cfg = cfg.get("mcp", {})
    mcp_host = mcp_cfg.get("host", "127.0.0.1")
    mcp_port = int(mcp_cfg.get("port", 55557))
    limits = cfg.get("limits", {})

    print(f"[cfg] db      = {db_path}")
    print(f"[cfg] roots   = {roots}")
    print(f"[cfg] mcp     = {mcp_host}:{mcp_port}  (UE 编辑器 UnrealMCP 插件, 未开时 AssetSage 会降级)")
    print(f"[cfg] query   = {user_input}")

    state = run_graph(
        user_input=user_input,
        roots=roots,
        db_path=db_path,
        mcp_host=mcp_host,
        mcp_port=mcp_port,
        planner_max_steps=limits.get("planner_max_steps", 6),
        codesage_max_steps=limits.get("codesage_max_steps", 8),
        assetsage_max_steps=limits.get("assetsage_max_steps", 6),
        verbose=args.verbose,
    )

    print("\n" + "=" * 60)
    print("最终答案")
    print("=" * 60)
    print(state.final_answer or "(空)")
    if state.error:
        print(f"\n[error] {state.error}")

    if args.dump_trace:
        trace = {
            "user_input": state.user_input,
            "forced_agent": state.forced_agent,
            "final_answer": state.final_answer,
            "error": state.error,
            "findings": state.findings,
            "tool_calls": [
                {"agent": t.agent, "tool": t.tool, "args": t.args,
                 "result": t.result, "ok": t.ok}
                for t in state.tool_calls
            ],
            "messages": [
                {"role": m.role, "content": m.content, "meta": m.meta}
                for m in state.messages
            ],
        }
        with open(args.dump_trace, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)
        print(f"\n[trace] 已写入 {args.dump_trace}")


if __name__ == "__main__":
    main()
