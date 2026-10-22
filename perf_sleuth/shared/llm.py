"""
LLM 薄壳。

本地实现（不再依赖 ue-callchain-explorer/agents/llm.py），加了：
- 一次自动 JSON 修复重试（"上次输出不是合法 JSON，请重新返回纯 JSON"）
- 明确指定 num_ctx 避免默认 2K 上下文
- 记录调用轮次到 stderr 供 debug
"""
import json
import os
import sys
import requests
from typing import Optional


DEFAULT_MODEL = os.environ.get("UE_PERF_SLEUTH_MODEL",
                               os.environ.get("UE_EXPLORER_MODEL", "qwen2.5:7b"))
DEFAULT_BASE = os.environ.get("UE_PERF_SLEUTH_LLM_BASE",
                              os.environ.get("UE_EXPLORER_LLM_BASE", "http://localhost:11434"))


def _extract_json(text: str) -> Optional[dict]:
    """从文本里挖 JSON。先直接 parse，失败则找首尾大括号。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def _post_ollama(messages, model: str, base_url: str, timeout: int, temperature: float) -> str:
    """底层 ollama /api/chat 调用，返回 message.content。"""
    resp = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": 8192,   # 默认 2K 太小，容易吃掉工具输出
            },
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("message", {}).get("content", "")


def chat_json(system: str, user: str,
              model: str = DEFAULT_MODEL,
              base_url: str = DEFAULT_BASE,
              timeout: int = 90,
              temperature: float = 0.0,
              retries: int = 1) -> dict:
    """
    强制 JSON 输出的 chat 调用。
    - 首次失败（非法 JSON / 空）自动重试一次，追加"请重新返回纯 JSON"提示
    - 全部失败返回 {"error": "..."}
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    last_content = ""
    last_error = ""

    for attempt in range(retries + 1):
        try:
            content = _post_ollama(messages, model, base_url, timeout, temperature)
        except Exception as e:
            last_error = f"llm call failed: {e}"
            continue

        if not content:
            last_error = "empty response"
        else:
            parsed = _extract_json(content)
            if parsed is not None and isinstance(parsed, dict):
                return parsed
            last_content = content
            last_error = "invalid json"

        # 修复重试：在对话里加一条"请重新输出合法 JSON"
        if attempt < retries:
            print(f"[llm] attempt {attempt+1} bad JSON, retrying...", file=sys.stderr)
            messages = messages + [
                {"role": "assistant", "content": last_content or "(empty)"},
                {"role": "user",
                 "content": "你刚才的输出不是合法的 JSON。请严格重新输出一个纯 JSON 对象，"
                            "不要任何前缀/后缀/markdown 代码块。"},
            ]

    return {"error": last_error, "raw": last_content[:500]}


def probe(base_url: str = DEFAULT_BASE) -> str:
    """检查 LLM 后端可用；返回错误串或空串。"""
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=3)
        resp.raise_for_status()
        return ""
    except Exception as e:
        return f"LLM backend @ {base_url} unavailable: {e}"
