# -*- coding: utf-8 -*-
"""Agent MCP 客户端 —— 搬自 AstrBot 的 MCP 集成。

把外部 MCP 服务器（文件系统、浏览器、数据库等现成工具生态）的工具接入同一个
ToolRegistry，和内置工具走完全相同的权限检查与执行路径。

配置文件 data/mcp_server.json，格式与 AstrBot / Claude Desktop 兼容：

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "F:/data"],
          "enabled": true
        },
        "remote": {
          "url": "https://example.com/sse",
          "enabled": false
        }
      }
    }

mcp 包未安装时整个模块降级为空实现，不影响其他工具。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import json
import os
import shutil
from contextlib import AsyncExitStack

from bot.agent import MCP_PREFIX, REGISTRY, AgentContext, ToolSpec

INIT_TIMEOUT = 60.0
CALL_TIMEOUT = 120.0

_settings_reader = None
_config_path = ""
_clients: dict[str, "MCPClient"] = {}

# MCP 的连接必须活在一个长期存在的事件循环里。
# 用 asyncio.run(refresh()) 是错的：函数一返回 loop 就关闭，stdio/SSE 传输的
# 后台读写 task 全被取消，而 _clients 里的对象还在、WebUI 还显示"已注册"，
# 之后任何工具调用都会失败。这里起一个专属线程跑常驻 loop，所有 MCP 操作
# 都用 run_coroutine_threadsafe 提交进去。
_mcp_loop: "asyncio.AbstractEventLoop | None" = None
_mcp_thread = None
_mcp_boot_lock = __import__("threading").Lock()


def _ensure_mcp_loop():
    """返回常驻 MCP 事件循环，必要时启动它。"""
    global _mcp_loop, _mcp_thread
    with _mcp_boot_lock:
        if _mcp_loop is not None and not _mcp_loop.is_closed() and _mcp_thread and _mcp_thread.is_alive():
            return _mcp_loop
        import threading
        loop = asyncio.new_event_loop()

        def _runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="AgentMcpLoop", daemon=True)
        thread.start()
        _mcp_loop, _mcp_thread = loop, thread
        return loop


def submit(coro, timeout: float = 300.0):
    """把一个 MCP 协程提交到常驻循环并等结果（同步调用方用）。"""
    loop = _ensure_mcp_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


def submit_nowait(coro):
    """提交但不等结果，返回 concurrent.futures.Future。"""
    return asyncio.run_coroutine_threadsafe(coro, _ensure_mcp_loop())
_registered: set[str] = set()
_lock = asyncio.Lock()

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_AVAILABLE = True
    _MCP_IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - 取决于用户是否装了 mcp
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None
    _MCP_AVAILABLE = False
    _MCP_IMPORT_ERROR = str(e)


def bind(settings_reader, config_path: str) -> None:
    global _settings_reader, _config_path
    _settings_reader = settings_reader
    _config_path = config_path


def is_available() -> tuple[bool, str]:
    if not _MCP_AVAILABLE:
        return False, f"未安装 mcp 依赖（pip install mcp）：{_MCP_IMPORT_ERROR}"
    return True, ""


def load_server_configs() -> dict[str, dict]:
    """读 mcp_server.json。文件不存在时返回空 dict，不报错。"""
    if not _config_path or not os.path.exists(_config_path):
        return {}
    try:
        with open(_config_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as e:
        print(f"[MCP] 配置文件解析失败: {e}")
        return {}
    servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        return {}
    return {
        str(name): cfg for name, cfg in servers.items()
        if isinstance(cfg, dict) and not str(name).startswith("_")
    }


def save_server_configs(servers: dict) -> None:
    os.makedirs(os.path.dirname(_config_path), exist_ok=True)
    tmp = f"{_config_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump({"mcpServers": servers}, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, _config_path)


class MCPClient:
    """一个 MCP 服务器的连接。stdio 走子进程，url 走 SSE。"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.session = None
        self.tools: list[dict] = []
        self._stack: AsyncExitStack | None = None
        self.last_error = ""

    async def connect(self) -> None:
        self._stack = AsyncExitStack()
        url = str(self.config.get("url", "") or "").strip()
        if url:
            # SSE 传输是可选依赖，单独 import 以便没装时给出明确原因
            from mcp.client.sse import sse_client
            headers = self.config.get("headers") if isinstance(self.config.get("headers"), dict) else None
            transport = await self._stack.enter_async_context(sse_client(url=url, headers=headers))
        else:
            command = str(self.config.get("command", "") or "").strip()
            if not command:
                raise ValueError("既没有 url 也没有 command，无法连接")
            # Windows 下 npx/uvx 是 .cmd，必须解析成绝对路径否则 FileNotFoundError
            resolved = shutil.which(command) or command
            args = self.config.get("args", [])
            args = [str(x) for x in args] if isinstance(args, list) else []
            env = dict(os.environ)
            if isinstance(self.config.get("env"), dict):
                env.update({str(k): str(v) for k, v in self.config["env"].items()})
            params = StdioServerParameters(command=resolved, args=args, env=env)
            transport = await self._stack.enter_async_context(stdio_client(params))

        read_stream, write_stream = transport
        self.session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = [
            {
                "name": str(t.name),
                "description": str(getattr(t, "description", "") or ""),
                "parameters": _normalize_schema(getattr(t, "inputSchema", None)),
            }
            for t in (listed.tools or [])
        ]

    async def call(self, tool_name: str, args: dict) -> str:
        if self.session is None:
            raise RuntimeError(f"MCP 服务器 {self.name} 未连接")
        result = await asyncio.wait_for(self.session.call_tool(tool_name, args), timeout=CALL_TIMEOUT)
        parts = []
        for item in (getattr(result, "content", None) or []):
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
            else:
                parts.append(str(item))
        if getattr(result, "isError", False):
            return "error: " + ("\n".join(parts) or "MCP 工具返回了错误但没有说明")
        return "\n".join(parts) if parts else "（MCP 工具执行成功，但没有返回内容）"

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack = None
        self.session = None


def _normalize_schema(schema) -> dict:
    """把 MCP 的 inputSchema 归一化成 OpenAI tools 认的 parameters。"""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    out.setdefault("type", "object")
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    # OpenAI 不认 $schema 之类的 JSON Schema 元字段，部分厂商会直接报错
    for key in ("$schema", "additionalProperties", "definitions", "$defs"):
        out.pop(key, None)
    return out


def _make_handler(server_name: str, tool_name: str):
    async def _handler(args: dict, ctx: AgentContext) -> str:
        client = _clients.get(server_name)
        if client is None or client.session is None:
            return (
                f"error: MCP 服务器 {server_name} 当前未连接。"
                "请告诉用户可以让管理员在 WebUI 的 Agent 页面检查 MCP 配置。"
            )
        ctx.say(f"mcp[{server_name}] {tool_name} {json.dumps(args, ensure_ascii=False)[:120]}", "AGENT")
        try:
            # 必须回到 MCP 常驻循环里调：session 的读写 task 都属于那个 loop，
            # 在消息线程自己的 loop 里直接 await 会撞上跨事件循环的原语。
            return await asyncio.wrap_future(submit_nowait(client.call(tool_name, args)))
        except asyncio.TimeoutError:
            return f"error: MCP 工具 {tool_name} 执行超过 {CALL_TIMEOUT:.0f} 秒未返回"
        except Exception as e:
            return f"error: MCP 工具 {tool_name} 调用失败：{type(e).__name__}: {e}"
    return _handler


_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_part(text: str, limit: int) -> str:
    """把名字里的非法字符换成下划线；被改动过就追加短哈希保证唯一。

    模型 API 要求函数名匹配 [A-Za-z0-9_-]{1,64}，而 MCP 服务器名可能是中文、
    工具名可能带点号。直接拼出去会让**整个请求**被 400 拒掉，一个坏名字
    连累本轮所有工具。
    """
    # 唯一性由 _tool_key 末尾的整体哈希保证，这里只负责合法化和截断
    return _NAME_SAFE.sub("_", str(text or ""))[:limit] or "x"


def _tool_key(server_name: str, tool_name: str) -> str:
    """生成模型可见的工具名。

    末尾固定带一段基于「服务器名 + 工具名」原文的哈希。只在名字被改写时才加
    哈希是不够的：('a', 'b__c') 和 ('a__b', 'c') 都合法、都不需要改写，
    但拼出来同名，后注册的会覆盖前一个，模型调用被路由到错误的服务器。
    """
    raw = repr((str(server_name), str(tool_name)))
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:8]
    # mcp__(5) + 两段各 22 + __(2) + _(1) + 8 = 60，留有余量
    return (f"{MCP_PREFIX}{_sanitize_part(server_name, 22)}"
            f"__{_sanitize_part(tool_name, 22)}_{digest}")


async def refresh() -> dict:
    """按配置重连所有启用的 MCP 服务器，并把它们的工具注册进 REGISTRY。

    返回每个服务器的状态摘要，供 WebUI 展示。
    """
    ok, reason = is_available()
    if not ok:
        return {"available": False, "error": reason, "servers": []}

    async with _lock:
        # 先摘掉上一轮注册的 MCP 工具，避免删掉服务器后工具还留在 schema 里
        for key in list(_registered):
            REGISTRY.unregister(key)
        _registered.clear()
        for client in list(_clients.values()):
            await client.close()
        _clients.clear()

        servers = load_server_configs()
        summary = []
        for name, config in servers.items():
            enabled = config.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() not in ("0", "false", "no", "off")
            if not enabled:
                summary.append({"name": name, "enabled": False, "connected": False,
                                "tools": [], "error": ""})
                continue

            client = MCPClient(name, config)
            try:
                await asyncio.wait_for(client.connect(), timeout=INIT_TIMEOUT)
            except Exception as e:
                client.last_error = f"{type(e).__name__}: {e}"
                print(f"[MCP] 服务器 {name} 连接失败: {client.last_error}")
                await client.close()
                summary.append({"name": name, "enabled": True, "connected": False,
                                "tools": [], "error": client.last_error})
                continue

            _clients[name] = client
            registered_names = []
            for item in client.tools:
                key = _tool_key(name, item["name"])
                REGISTRY.register(ToolSpec(
                    name=key,
                    description=f"[MCP:{name}] {item['description']}",
                    parameters=item["parameters"],
                    handler=_make_handler(name, item["name"]),
                    # MCP 工具能力未知（可能读写文件、访问网络），统一按 admin 起步
                    level="admin",
                    untrusted_output=True,
                    timeout=CALL_TIMEOUT,
                    # MCP 的能力来自外部服务器，无法可靠判断只读/写入。按最严格策略
                    # 一律视为有副作用，避免总结失败后换渠道重放未知操作。
                    side_effect=True,
                ))
                _registered.add(key)
                registered_names.append(key)
            print(f"[MCP] 服务器 {name} 已连接，注册 {len(registered_names)} 个工具")
            summary.append({"name": name, "enabled": True, "connected": True,
                            "tools": registered_names, "error": ""})

        return {"available": True, "error": "", "servers": summary}


def shutdown_sync(timeout: float = 5.0) -> None:
    """进程退出时同步关掉所有 MCP 连接并停掉专属事件循环。

    只在循环还活着时做事；没启用过 MCP 时这是个空操作。
    """
    global _mcp_loop, _mcp_thread
    loop, thread = _mcp_loop, _mcp_thread
    if loop is None or loop.is_closed():
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(shutdown(), loop)
        fut.result(timeout=timeout)
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    _mcp_loop, _mcp_thread = None, None


def registered_tool_names() -> list[str]:
    return sorted(_registered)


async def shutdown() -> None:
    async with _lock:
        for key in list(_registered):
            REGISTRY.unregister(key)
        _registered.clear()
        for client in list(_clients.values()):
            await client.close()
        _clients.clear()
