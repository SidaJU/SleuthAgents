"""
UnrealMCP TCP 客户端 —— 连到 UE 编辑器里跑的 UnrealMCP 插件。

协议:
- TCP 127.0.0.1:55557 (端口在 UnrealMCP/EpicUnrealMCPBridge.cpp 的 MCP_SERVER_PORT 定义)
- 请求: 一条 JSON `{"type": "<command_type>", "params": {...}}` 直接发字节流
- 响应: 一条 JSON（可能很大，UnrealMCP 支持 1MB+ payload）
- 服务端配置了 TCP_NODELAY + KEEPALIVE，30s 收发超时

用法:
    client = UnrealMCPClient()  # 默认 127.0.0.1:55557
    resp = client.call("get_asset_info", {"asset_path": "/Game/..."})
    if resp.get("status") == "error":
        ...
    else:
        ...

约定:
- call() 失败（连不上/超时/非 JSON）返回 {"status": "error", "error": "..."}，不抛异常
- ping() 快速探测服务可用性
"""
import json
import socket
from typing import Any, Dict, Optional


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 55557


class UnrealMCPClient:
    """一次一个请求的短连接客户端。UnrealMCP 是单客户端串行执行的，短连接最省心。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 connect_timeout: float = 3.0,
                 recv_timeout: float = 30.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.recv_timeout = recv_timeout

    # ---- 底层：一次请求-响应 ----
    def _send_recv(self, payload: bytes) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout)
        try:
            sock.connect((self.host, self.port))
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            sock.close()
            raise ConnectionError(f"connect {self.host}:{self.port} failed: {e}") from e

        try:
            sock.settimeout(self.recv_timeout)
            sock.sendall(payload)
            # UnrealMCP 服务端接收完整 JSON 后回复；这里读到 EOF 或超时
            chunks = []
            while True:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    if chunks:
                        break  # 有数据就返回，别死等
                    raise
                if not data:
                    break
                chunks.append(data)
                # 尝试提前判定：字节流是否是一个完整 JSON
                try:
                    text = b"".join(chunks).decode("utf-8", errors="replace")
                    json.loads(text)
                    return text
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            return b"".join(chunks).decode("utf-8", errors="replace")
        finally:
            sock.close()

    def call(self, command_type: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        发一个 UnrealMCP 命令，返回解析后的 dict。
        网络/协议异常返回 {"status": "error", "error": "..."}。
        """
        req = json.dumps({"type": command_type, "params": params or {}}, ensure_ascii=False)
        try:
            text = self._send_recv(req.encode("utf-8"))
        except ConnectionError as e:
            return {"status": "error", "error": str(e)}
        except socket.timeout:
            return {"status": "error", "error": f"recv timeout ({self.recv_timeout}s)"}
        except Exception as e:
            return {"status": "error", "error": f"unexpected: {e}"}

        if not text.strip():
            return {"status": "error", "error": "empty response"}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            return {"status": "error", "error": f"invalid json: {e}", "raw": text[:500]}

    # ---- 便捷探测 ----
    def ping(self) -> str:
        """返回错误串（空串 = 服务可用）。"""
        r = self.call("ping")
        if r.get("status") == "error":
            return r.get("error", "unknown")
        return ""
