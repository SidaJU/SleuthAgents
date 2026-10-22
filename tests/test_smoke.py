"""
Smoke test —— 不调 LLM，验证：
1. 模块导入
2. 索引桥接可用
3. 强制路由规则识别（三向: codesage / assetsage / 无）
4. State 流转（Planner ↔ CodeSage ↔ AssetSage）用 mock LLM
5. UnrealMCP client 不可达时优雅返回
"""
import os, sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..")))

from perf_sleuth.state import State, HandoffRequest
from perf_sleuth.agents.planner import detect_forced_agent


class ForcedRoutingTests(unittest.TestCase):
    def test_forced_code_by_keyword_zh(self):
        self.assertEqual(detect_forced_agent("用代码专家搜 SetHiddenInGame"), "codesage")
        self.assertEqual(detect_forced_agent("我想查代码里 xxx 的调用"), "codesage")
        self.assertEqual(detect_forced_agent("看源码：UDamageComponent"), "codesage")

    def test_forced_code_by_tool_name(self):
        self.assertEqual(detect_forced_agent("ripgrep 一下 SetHiddenInGame"), "codesage")
        self.assertEqual(detect_forced_agent("Please grep for CanBeOccluded"), "codesage")

    def test_forced_asset_by_keyword(self):
        self.assertEqual(detect_forced_agent("用资产专家查 SM_Foo 的 Nanite 设置"), "assetsage")
        self.assertEqual(detect_forced_agent("查编辑器里可见的静态网格"), "assetsage")
        self.assertEqual(detect_forced_agent("查蓝图 BP_Bar 的组件"), "assetsage")
        self.assertEqual(detect_forced_agent("查 nanite 状态"), "assetsage")

    def test_asset_priority_over_code(self):
        # 同时命中时资产优先（更具体）
        self.assertEqual(
            detect_forced_agent("查代码看看 nanite 是否开启，再查资产"),
            "assetsage",
        )

    def test_no_force(self):
        self.assertEqual(detect_forced_agent("为什么履带一直发 query"), "")
        self.assertEqual(detect_forced_agent("UPrimitiveComponent"), "")
        self.assertEqual(detect_forced_agent("RDestruction 模块干什么的"), "")


class StateFlowTests(unittest.TestCase):
    """用 mock LLM 验证 Planner ↔ 下游 Agent 的编排顺序。"""

    def test_forced_route_shortcut_code(self):
        from perf_sleuth.agents import planner
        s = State(user_input="用代码专家查 SetHiddenInGame")
        planner.run(s, verbose=False)
        self.assertEqual(s.route, "codesage")
        self.assertIsNotNone(s.current_handoff)
        self.assertEqual(s.forced_agent, "codesage")

    def test_forced_route_shortcut_asset(self):
        from perf_sleuth.agents import planner
        s = State(user_input="用资产专家查 SM_BMP3_Track 的 Nanite 设置")
        planner.run(s, verbose=False)
        self.assertEqual(s.route, "assetsage")
        self.assertEqual(s.forced_agent, "assetsage")

    def test_planner_finalize_shortcircuit(self):
        from perf_sleuth.agents import planner as p
        s = State(user_input="你是谁")
        fake = {"thought": "打招呼", "action": "finalize",
                "args": {"answer": "我是多 Agent 系统的 Planner。"}}
        with patch.object(p, "chat_json", return_value=fake):
            p.run(s, verbose=False)
        self.assertEqual(s.route, "done")
        self.assertIn("Planner", s.final_answer)

    def test_planner_handoff_codesage(self):
        from perf_sleuth.agents import planner as p
        s = State(user_input="UPrimitiveComponent 定义在哪")
        fake = {"thought": "查符号", "action": "handoff_codesage",
                "args": {"task": "查找 UPrimitiveComponent 的类定义", "context": ""}}
        with patch.object(p, "chat_json", return_value=fake):
            p.run(s, verbose=False)
        self.assertEqual(s.route, "codesage")
        self.assertEqual(s.current_handoff.task, "查找 UPrimitiveComponent 的类定义")

    def test_planner_handoff_assetsage(self):
        from perf_sleuth.agents import planner as p
        s = State(user_input="SM_Foo 的 Nanite 是否启用")
        fake = {"thought": "问资产", "action": "handoff_assetsage",
                "args": {"task": "查 SM_Foo 的 Nanite 属性", "context": ""}}
        with patch.object(p, "chat_json", return_value=fake):
            p.run(s, verbose=False)
        self.assertEqual(s.route, "assetsage")
        self.assertEqual(s.current_handoff.to, "assetsage")


class BridgeTests(unittest.TestCase):
    def test_resolve_db_or_skip(self):
        from perf_sleuth.shared.explorer_bridge import resolve_db_path
        try:
            p = resolve_db_path("")
            self.assertTrue(os.path.exists(p))
            print(f"\n  [ok] db 已就绪: {p}")
        except FileNotFoundError as e:
            self.skipTest(f"无索引 db：{e}")


class MCPClientTests(unittest.TestCase):
    """UnrealMCP client 连不上时应该返回 error dict，不抛异常。"""

    def test_call_returns_error_when_ue_down(self):
        from perf_sleuth.shared.mcp_client import UnrealMCPClient
        # 用一个不可能开的端口
        c = UnrealMCPClient(host="127.0.0.1", port=1, connect_timeout=0.5)
        r = c.call("ping")
        self.assertEqual(r.get("status"), "error")
        self.assertIn("error", r)

    def test_ping_returns_error_string_when_ue_down(self):
        from perf_sleuth.shared.mcp_client import UnrealMCPClient
        c = UnrealMCPClient(host="127.0.0.1", port=1, connect_timeout=0.5)
        err = c.ping()
        self.assertTrue(err)  # 非空 = 错误


if __name__ == "__main__":
    unittest.main(verbosity=2)
