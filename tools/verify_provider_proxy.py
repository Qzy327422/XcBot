# -*- coding: utf-8 -*-
"""B 组验证：提供商级 HTTP 代理是否真的生效。

不联网。做法是在本机同时起两个服务：

  - 假 LLM 源站（origin）：应答 /chat/completions、/embeddings、/models
  - 记账代理（proxy）：转发请求并记下「谁经我手」

然后分别调用项目里的六条链路，看请求落在哪一侧：
落在 proxy 上 = 代理生效；直接落在 origin 上 = 绕过了代理。

同时把环境变量 HTTP_PROXY/HTTPS_PROXY 指向一个**不存在的**端口。
如果某条链路读了环境变量（trust_env 泄漏），它会连接失败而不是静默走对，
这样"留空不回退全局/环境代理"这条承诺才算真的被验证过，而不是只看代码。

用法：python tools/verify_provider_proxy.py
退出码 0 = 全部符合预期。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在导入项目模块之前设置：httpx / urllib 都在构造时读环境变量。
# 指向一个几乎不可能被占用的端口，任何"误读环境代理"的行为都会立刻连接失败。
BOGUS_PROXY = "http://127.0.0.1:9"
os.environ["HTTP_PROXY"] = BOGUS_PROXY
os.environ["HTTPS_PROXY"] = BOGUS_PROXY
os.environ["http_proxy"] = BOGUS_PROXY
os.environ["https_proxy"] = BOGUS_PROXY

# 必须清掉 no_proxy：本脚本的假源站在 127.0.0.1 上，而很多环境（含本机 shell）
# 默认带 no_proxy=localhost,127.0.0.1,::1。urllib 的 ProxyHandler.proxy_open
# 里有一句无条件的 `if req.host and proxy_bypass(req.host): return None`，
# proxy_bypass 在检测到环境里有代理时会读 no_proxy——于是显式传进去的代理字典
# 会被环境变量否决，请求直达 127.0.0.1 源站。留着它测的就不是「代理是否生效」，
# 而是「no_proxy 是否命中」，三条 urllib 链路会假失败。
# 顺带记下真实行为供 check_no_proxy_precedence 断言。
_INHERITED_NO_PROXY = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
for _k in ("no_proxy", "NO_PROXY"):
    os.environ.pop(_k, None)

results: list[tuple[str, str, str]] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


ORIGIN_HITS: list[str] = []
PROXY_HITS: list[str] = []
_hits_lock = threading.Lock()

FAKE_COMPLETION = {
    "id": "x", "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"total_tokens": 3, "prompt_tokens": 2, "completion_tokens": 1},
}
FAKE_EMBEDDING = {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}
FAKE_MODELS = {"data": [{"id": "fake-model"}]}


def _body_for(path: str) -> dict:
    if "/embeddings" in path:
        return FAKE_EMBEDDING
    if "/models" in path:
        return FAKE_MODELS
    return FAKE_COMPLETION


class OriginHandler(BaseHTTPRequestHandler):
    """假 LLM 源站。记下每一次直达它的请求。"""

    protocol_version = "HTTP/1.1"

    def _record_and_reply(self):
        with _hits_lock:
            ORIGIN_HITS.append(self.path)
        payload = json.dumps(_body_for(self.path)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._record_and_reply()

    def do_GET(self):
        self._record_and_reply()

    def log_message(self, *args):
        pass


class ProxyHandler(BaseHTTPRequestHandler):
    """记账正向代理。

    普通 HTTP 代理收到的是绝对 URI（http://host:port/path），据此转发。
    这里只需要证明"请求经过了我"，所以转发给源站时带一个标记头，
    源站那边的 ORIGIN_HITS 也会记一笔——用 PROXY_HITS 判定是否经代理。
    """

    protocol_version = "HTTP/1.1"

    def _forward(self, method: str, body: bytes | None):
        with _hits_lock:
            PROXY_HITS.append(f"{method} {self.path}")
        try:
            req = urllib.request.Request(self.path, data=body, method=method)
            for name in ("Content-Type", "Authorization"):
                if self.headers.get(name):
                    req.add_header(name, self.headers[name])
            # 关键：转发时用一个显式禁用代理的 opener，否则会把请求又交回自己。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=10) as upstream:
                payload = upstream.read()
                status = upstream.status
        except Exception as e:  # noqa: BLE001
            payload = json.dumps({"error": str(e)}).encode()
            status = 502
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        self._forward("POST", body)

    def do_GET(self):
        self._forward("GET", None)

    def log_message(self, *args):
        pass


def serve(handler_cls, port: int) -> ThreadingHTTPServer:
    # 必须多线程：httpx 用 HTTP/1.1 keep-alive，单线程服务器会被一条空闲的
    # 长连接占死，后续请求（尤其代理转发到源站那一跳）全部超时。
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def reset_hits() -> None:
    with _hits_lock:
        ORIGIN_HITS.clear()
        PROXY_HITS.clear()


def snapshot() -> tuple[list[str], list[str]]:
    with _hits_lock:
        return list(ORIGIN_HITS), list(PROXY_HITS)


# ==================== 各条链路 ====================

def check_normalize_proxy_url() -> None:
    from bot.llm_config import normalize_proxy_url as n

    ok("proxy 补全协议", n("127.0.0.1:7890") == "http://127.0.0.1:7890")
    ok("proxy 去引号", n('"http://127.0.0.1:7890"') == "http://127.0.0.1:7890")
    ok("proxy 留空即直连", n("") == "" and n("   ") == "")
    ok("proxy 拒绝 socks", n("socks5://127.0.0.1:1080") == "")
    ok("proxy 拒绝其它协议", n("ftp://x") == "")


def check_httpx_client_trust_env(proxy_url: str, origin: str) -> None:
    """B3 的核心：留空时必须显式禁用环境代理。

    环境变量已被指向 127.0.0.1:9（无人监听）。如果 trust_env 泄漏，
    这次请求会连接失败；只有真的禁用了才能打到 origin。
    """
    from main import get_provider_http_client

    client = get_provider_http_client("", 10)
    ok("留空返回显式客户端而非 None", client is not None,
       "None 会让 SDK 自建 trust_env=True 的客户端")
    if client is None:
        return
    ok("留空客户端 trust_env=False", getattr(client, "trust_env", True) is False)

    reset_hits()
    try:
        resp = client.post(f"{origin}/chat/completions", json={"model": "m", "messages": []})
        reached = resp.status_code == 200
        err = ""
    except Exception as e:  # noqa: BLE001
        reached = False
        err = str(e)
    o, p = snapshot()
    ok("留空 = 直连，不读环境 HTTP_PROXY", reached and len(o) == 1 and not p,
       err or f"origin={o} proxy={p}")

    client2 = get_provider_http_client(proxy_url, 10)
    ok("配了代理返回独立客户端", client2 is not None and client2 is not client)
    reset_hits()
    try:
        resp = client2.post(f"{origin}/chat/completions", json={"model": "m", "messages": []})
        reached = resp.status_code == 200
        err = ""
    except Exception as e:  # noqa: BLE001
        reached = False
        err = str(e)
    o, p = snapshot()
    ok("配了代理的请求经过代理", reached and len(p) == 1, err or f"origin={o} proxy={p}")

    client3 = get_provider_http_client(proxy_url, 10)
    ok("同代理复用同一客户端", client3 is client2)


def check_embeddings(proxy_url: str, origin: str) -> None:
    """知识库嵌入链路（urllib + ProxyHandler）。"""
    from bot.knowledge_base import _embed_with_key

    reset_hits()
    try:
        _embed_with_key(["hi"], {"base_url": origin, "http_proxy": ""}, "m", "sk-x")
        err = ""
    except Exception as e:  # noqa: BLE001
        err = str(e)
    o, p = snapshot()
    ok("嵌入：留空 = 直连", not err and len(o) == 1 and not p, err or f"origin={o} proxy={p}")

    reset_hits()
    try:
        _embed_with_key(["hi"], {"base_url": origin, "http_proxy": proxy_url}, "m", "sk-x")
        err = ""
    except Exception as e:  # noqa: BLE001
        err = str(e)
    o, p = snapshot()
    ok("嵌入：配了代理走代理", not err and len(p) == 1, err or f"origin={o} proxy={p}")


def check_attempt_identity() -> None:
    """A6：轮换身份必须含代理维度。

    同 base_url + 同 model + 同 key 但不同代理，是两个不同的可尝试组合。
    identity 不含代理时，第二个会被 tried_keys 判为已试过而永久跳过。
    """
    from key_manager import key_manager

    a = key_manager.make_attempt_identity("https://x/v1", "sk-1", "m", "http://127.0.0.1:1")
    b = key_manager.make_attempt_identity("https://x/v1", "sk-1", "m", "http://127.0.0.1:2")
    c = key_manager.make_attempt_identity("https://x/v1", "sk-1", "m", "")
    ok("轮换身份区分不同代理", a != b, f"a={a} b={b}")
    ok("轮换身份区分代理与直连", a != c)
    d = key_manager.make_attempt_identity("https://x/v1", "sk-1", "m", "http://127.0.0.1:1")
    ok("同组合身份稳定", a == d)


def check_client_pool_cache_key(proxy_url: str) -> None:
    """A8：_get_client 的 cache_key 含代理，改代理不复用旧连接。"""
    import main

    cls = main.LimitedDeepSeekContext
    ctx = object.__new__(cls)
    # _client_pool 是类级共享字典，这里在实例上遮蔽一份，避免污染真实进程池。
    ctx._client_pool = {}
    c1 = cls._get_client(ctx, "https://x/v1", "sk-1", 10, "")
    c2 = cls._get_client(ctx, "https://x/v1", "sk-1", 10, proxy_url)
    c3 = cls._get_client(ctx, "https://x/v1", "sk-1", 10, "")
    ok("改代理不复用旧客户端", c1 is not c2)
    ok("同代理复用客户端", c1 is c3)
    keys = list(ctx._client_pool.keys())
    ok("cache_key 含代理", any(proxy_url in k for k in keys), f"keys={keys}")

    # 压缩摘要走的是另一个类的同名方法，单独验一遍，避免只改了一处。
    ccls = main.ContextCompressor
    cctx = object.__new__(ccls)
    cctx._client_pool = {}
    d1 = ccls._get_client(cctx, "https://x/v1", "sk-1", 10, "")
    d2 = ccls._get_client(cctx, "https://x/v1", "sk-1", 10, proxy_url)
    ok("压缩摘要：改代理不复用旧客户端", d1 is not d2)
    ok("压缩摘要：cache_key 含代理", any(proxy_url in k for k in cctx._client_pool))


def check_global_proxy_isolation() -> None:
    """B5：全局代理不再影响模型调用，只管 GitHub / 插件市场 / Agent 联网。"""
    import webui

    has_global = hasattr(webui, "_make_opener")
    has_provider = hasattr(webui, "_make_provider_opener")
    ok("全局与提供商 opener 分离", has_global and has_provider)
    if has_provider:
        import inspect
        src = inspect.getsource(webui._make_provider_opener)
        ok("提供商 opener 留空时显式禁用代理", "ProxyHandler({})" in src or "ProxyHandler({}" in src,
           "必须显式空字典，不能省略 handler")


def check_bypass_save_validation() -> None:
    """B2：绕过 WebUI 保存校验时会发生什么。

    WebUI 保存分支（webui.py:1496-1503）遇到无效代理会抛错拦截。
    但手改 config.json 或旧配置迁移不经过那里，只经过 llm_config 的归一化，
    那条路径对 socks5 是「静默返回空串」——结果是静默直连，不是报错。
    这里把这个行为固定下来，避免以后误以为它会拦。
    """
    from bot.llm_config import build_all_provider_endpoints

    others = {
        "llm_providers": [{
            "id": "p", "base_url": "https://x/v1", "keys": ["sk-live-1"],
            "http_proxy": "socks5://127.0.0.1:1080",
            "models": [{"name": "m", "enabled": True}],
        }],
        "llm_rotation": [{"provider_id": "p", "model": "m"}],
    }
    eps = build_all_provider_endpoints(others)
    ok("手改配置写 socks 会静默降级为直连", len(eps) == 1 and eps[0]["http_proxy"] == "",
       f"eps={[e.get('http_proxy') for e in eps]}（已知行为：不抛错，只清空）")


def check_provider_opener(proxy_url: str, origin: str) -> None:
    """模型探测链路（webui.py 的 /models GET，直接用 _make_provider_opener）。"""
    import webui

    for label, proxy, expect_proxy in (("留空 = 直连", "", False), ("配了代理走代理", proxy_url, True)):
        reset_hits()
        try:
            with webui._make_provider_opener(proxy).open(f"{origin}/models", timeout=10) as resp:
                got = resp.status == 200
            err = ""
        except Exception as e:  # noqa: BLE001
            got, err = False, str(e)
        o, p = snapshot()
        cond = got and (bool(p) == expect_proxy) and (len(o) == 1)
        ok(f"模型探测：{label}", cond, err or f"origin={o} proxy={p}")


def check_chatroom(proxy_url: str, origin: str) -> None:
    """聊天室链路（webui._chatroom_complete，非流式）。

    真实实现从 config.json 读提供商，这里替换 _chatroom_rotation_endpoints
    让它指向假源站；被验证的仍是 _chatroom_complete 里那句 _make_provider_opener(ep)。
    """
    import webui

    original = webui._chatroom_rotation_endpoints
    try:
        for label, proxy, expect_proxy in (("留空 = 直连", "", False), ("配了代理走代理", proxy_url, True)):
            ep = {
                "provider_id": "fake", "base_url": origin, "model": "m",
                "display_model": "fake/m", "keys": ["sk-x"],
                "http_proxy": proxy, "timeout_seconds": 10,
            }
            webui._chatroom_rotation_endpoints = lambda _m, _ep=ep: [_ep]
            reset_hits()
            try:
                content, _reasoning = webui._chatroom_complete("fake/m", [{"role": "user", "content": "hi"}])
                got = content == "ok"
                err = ""
            except Exception as e:  # noqa: BLE001
                got, err = False, str(e)
            o, p = snapshot()
            cond = got and (bool(p) == expect_proxy) and (len(o) == 1)
            ok(f"聊天室：{label}", cond, err or f"origin={o} proxy={p}")
    finally:
        webui._chatroom_rotation_endpoints = original


def main() -> int:
    origin_port, proxy_port = free_port(), free_port()
    origin = f"http://127.0.0.1:{origin_port}"
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    print(f"origin = {origin}")
    print(f"proxy  = {proxy_url}")
    print(f"env HTTP_PROXY = {BOGUS_PROXY}  (无人监听，用于检测 trust_env 泄漏)\n")

    s1 = serve(OriginHandler, origin_port)
    s2 = serve(ProxyHandler, proxy_port)
    try:
        check_normalize_proxy_url()
        check_attempt_identity()
        check_httpx_client_trust_env(proxy_url, origin)
        check_embeddings(proxy_url, origin)
        check_client_pool_cache_key(proxy_url)
        check_global_proxy_isolation()
        check_provider_opener(proxy_url, origin)
        check_chatroom(proxy_url, origin)
        check_bypass_save_validation()
    finally:
        s1.shutdown()
        s2.shutdown()

    fails = [r for r in results if r[0] == "FAIL"]
    print("\n=== SUMMARY ===")
    print(f"total={len(results)} pass={len(results) - len(fails)} fail={len(fails)}")
    if fails:
        print("FAILURES:")
        for _, name, detail in fails:
            print(f"  - {name}: {detail}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



