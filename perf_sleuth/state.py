"""
共享 state —— Planner 和 CodeSage 之间用它交接。

刻意保持"扁平字典"结构，不引 Pydantic / dataclass，方便打印和调试。
"""
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


@dataclass
class Message:
    """Agent 内部/之间的一条消息，用来贯穿追溯。"""
    role: str          # "user" | "planner" | "codesage" | "tool"
    content: str
    meta: Dict = field(default_factory=dict)


@dataclass
class ToolCall:
    """一次工具调用的记录，用于 ReAct 循环回放和最终报告。"""
    agent: str         # 谁调的："planner" | "codesage"
    tool: str          # 工具名
    args: Dict
    result: str        # 已截断的可读结果
    ok: bool = True


@dataclass
class HandoffRequest:
    """Planner 把任务交给 CodeSage 的一次请求。"""
    to: str            # "codesage"（B 阶段固定）
    task: str          # 自然语言子任务
    context: str = ""  # 补充上下文（原始输入的相关片段等）


@dataclass
class State:
    """
    整个会话的共享 state。
    Planner 和 CodeSage 都读写它 —— 用 `route` 字段决定下一步跑谁。
    """
    user_input: str
    route: str = "planner"                 # 下一个跑的 agent："planner" | "codesage" | "done"
    forced_agent: Optional[str] = None     # 用户显式指定的 agent（跳过 Planner 自动规划）
    current_handoff: Optional[HandoffRequest] = None
    findings: List[Dict] = field(default_factory=list)  # CodeSage 每次返回时追加
    messages: List[Message] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    final_answer: str = ""
    error: str = ""

    def log(self, role: str, content: str, **meta):
        self.messages.append(Message(role=role, content=content, meta=meta))

    def add_tool_call(self, agent: str, tool: str, args: Dict, result: str, ok: bool = True):
        self.tool_calls.append(ToolCall(agent=agent, tool=tool, args=args, result=result, ok=ok))
