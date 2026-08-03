# -*- coding: utf-8 -*-
"""XcBot Agent 内置工具集 —— 与 QQ 无关的通用工具。

这里只放不依赖 main.py 全局状态的工具（搜索、抓网页、时间、计算）。
需要 actions / event 的 QQ 操作类工具在 main.py 里注册，因为它们依赖主程序
的运行时对象。
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
import urllib.parse
from datetime import datetime, timedelta, timezone

import aiohttp

from bot.agent import AgentContext, tool
from bot.safe_calc import CalcError, safe_eval

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_CST = timezone(timedelta(hours=8))

# main.py 在启动时把 get_runtime_setting 注入进来，让工具能读 config.json 里的
# 代理和搜索 API key，而不必 import main（会循环依赖）。
_settings_reader = None


def bind_settings_reader(reader) -> None:
    global _settings_reader
    _settings_reader = reader


def _setting(path: str, default=None):
    if callable(_settings_reader):
        try:
            value = _settings_reader(path, default)
            return default if value is None else value
        except Exception:
            return default
    return default


def _proxy() -> str | None:
    return str(_setting("Others.http_proxy", "") or "").strip() or None


def _strip_html(raw: str) -> str:
    """把 HTML 压成纯文本。够用即可，不引入 bs4 之类的新依赖。"""
    text = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _is_forbidden_ip(ip: str) -> bool:
    """判断一个 IP 是否属于禁止访问的范围。

    只比对主机名字符串是拦不住的：localhost. 会解析到 ::1，
    ::ffff:127.0.0.1 是 IPv4 映射地址，2130706433 / 0x7f000001 是
    127.0.0.1 的十进制与十六进制写法，公网域名也可以解析到内网。
    所以必须解析成 IP 之后按网段判断。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # 解析不出来就不放行
    # IPv4 映射地址（::ffff:127.0.0.1）要还原成 IPv4 再判断
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(
        addr.is_loopback or addr.is_private or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
        or (addr.version == 6 and (addr.is_site_local or str(addr).startswith("fd")))
    )


def resolve_and_check_host(host: str) -> tuple[list[str], str]:
    """把主机名解析成 IP 列表并逐个校验。返回 (ip 列表, 错误说明)。

    所有解析结果都必须通过：DNS 轮询可能同时返回公网和内网地址，
    只查第一个等于给了绕过窗口。
    """
    host = str(host or "").strip().strip("[]")
    if not host:
        return [], "缺少主机名"
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:
        return [], f"主机名无法解析：{e}"
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        return [], "主机名解析结果为空"
    for ip in ips:
        if _is_forbidden_ip(ip):
            return ips, f"目标 {host} 解析到受限地址 {ip}（内网/本机/保留段）"
    return ips, ""


# 单次响应最多读多少字节。不设上限的话 resp.text() 会把整个响应体拉进内存，
# 一个几百 MB 的下载链接就能把机器人打爆。
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


async def _read_capped(resp, limit: int = MAX_RESPONSE_BYTES) -> str:
    """按字节上限流式读取响应体并解码。超限就截断，不报错。

    必须边读边数：先 await resp.text() 再判断长度是没用的，
    那时内存已经吃进去了。
    """
    chunks = []
    size = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > limit:
            chunks.append(chunk[: max(0, limit - (size - len(chunk)))])
            break
        chunks.append(chunk)
    raw = b"".join(chunks)
    encoding = resp.charset or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        return raw.decode("utf-8", errors="replace")


async def _fetch(url: str, *, method: str = "GET", timeout: float = 20.0,
                 headers: dict | None = None, check_host: bool = False,
                 max_redirects: int = 3, **kwargs) -> tuple[int, str]:
    """发一个 HTTP 请求。

    check_host=True 时对每一跳都做 DNS 解析后的内网校验，并手动跟随重定向——
    交给 aiohttp 自动跳转的话，第一跳过了检查、后面被 302 引到内网就拦不住了。
    搜索 API 这类固定域名不需要开（省一次 DNS），面向用户输入的一律要开。
    """
    merged = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    merged.update(headers or {})

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        current = url
        for _ in range(max(1, max_redirects + 1)):
            if check_host:
                parsed = urllib.parse.urlparse(current)
                if parsed.scheme not in ("http", "https"):
                    raise RuntimeError("只支持 http/https 链接")
                _, err = resolve_and_check_host(parsed.hostname or "")
                if err:
                    raise RuntimeError(err)
            async with session.request(method, current, headers=merged, proxy=_proxy(),
                                       allow_redirects=not check_host, **kwargs) as resp:
                if check_host and resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if not location:
                        return resp.status, await _read_capped(resp)
                    current = urllib.parse.urljoin(current, location)
                    continue
                return resp.status, await _read_capped(resp)
        raise RuntimeError(f"重定向次数超过 {max_redirects} 次")


# ==================== 时间 ====================

@tool(
    name="get_current_time",
    description="获取当前的真实日期和时间（北京时间）。当用户问今天几号、现在几点、今天星期几，或你需要计算日期差时使用。",
    parameters={"type": "object", "properties": {}},
    level="user",
)
async def _tool_current_time(args: dict, ctx: AgentContext) -> str:
    now = datetime.now(_CST)
    weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
    return now.strftime(f"%Y年%m月%d日 %H:%M:%S {weekday}（北京时间 UTC+8）")


# ==================== 计算 ====================



@tool(
    name="calculate",
    description="计算一个数学表达式并返回精确结果。涉及具体数字的加减乘除、百分比、幂运算时使用，不要自己心算。",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，只允许数字和 + - * / % ( ) ** 运算符，例如 (1234+567)*8.5/3",
            },
        },
        "required": ["expression"],
    },
    level="user",
    timeout=5.0,
    # 求值是同步的：不放线程池的话，一个大数幂运算会把事件循环卡到算完为止，
    # 声明的超时和 /停止 都不生效
    cpu_bound=True,
)
def _tool_calculate(args: dict, ctx: AgentContext) -> str:
    # 求值走 bot.safe_calc 的 AST 白名单，不用 eval：
    # 正则黑名单挡不住 9**(+999999999)、9**(9**9)、(9)**(999999) 这类写法，
    # 而 eval 一旦开始算超大幂就会在超时生效前吃光内存。
    try:
        value = safe_eval(args.get("expression", ""))
    except CalcError as e:
        return f"error: {e}"
    except Exception as e:
        return f"error: 计算失败：{type(e).__name__}: {e}"
    expr = str(args.get("expression", "") or "").strip()
    return f"{expr} = {value}"


# ==================== 联网搜索 ====================

async def _search_tavily(query: str, count: int, api_key: str) -> str:
    status, body = await _fetch(
        "https://api.tavily.com/search",
        method="POST",
        timeout=25.0,
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "api_key": api_key,
            "query": query,
            "max_results": count,
            "search_depth": "basic",
            "include_answer": True,
        }),
    )
    if status != 200:
        raise RuntimeError(f"Tavily 返回 HTTP {status}")
    data = json.loads(body)
    lines = []
    if data.get("answer"):
        lines.append(f"【摘要】{data['answer']}")
    for i, item in enumerate(data.get("results") or [], start=1):
        lines.append(
            f"{i}. {item.get('title', '')}\n   {item.get('url', '')}\n   "
            f"{str(item.get('content', '') or '')[:300]}"
        )
    return "\n".join(lines) if lines else "没有找到相关结果。"


async def _search_bocha(query: str, count: int, api_key: str) -> str:
    status, body = await _fetch(
        "https://api.bochaai.com/v1/web-search",
        method="POST",
        timeout=25.0,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=json.dumps({"query": query, "count": count, "summary": True}),
    )
    if status != 200:
        raise RuntimeError(f"博查 返回 HTTP {status}")
    pages = ((json.loads(body).get("data") or {}).get("webPages") or {}).get("value") or []
    lines = [
        f"{i}. {p.get('name', '')}\n   {p.get('url', '')}\n   "
        f"{str(p.get('summary') or p.get('snippet') or '')[:300]}"
        for i, p in enumerate(pages, start=1)
    ]
    return "\n".join(lines) if lines else "没有找到相关结果。"


async def _search_baidu(query: str, count: int) -> str:
    """无需 API key 的国内兜底搜索。DuckDuckGo 在国内网络常连不上，故保留这一路。"""
    status, body = await _fetch(
        "https://www.baidu.com/s?" + urllib.parse.urlencode({"wd": query, "rn": max(count, 10)}),
        timeout=25.0,
        headers={"Referer": "https://www.baidu.com/"},
    )
    if status != 200:
        raise RuntimeError(f"百度 返回 HTTP {status}")

    lines = []
    # 百度结果页结构多变，按 result 容器切块后各自提取标题/链接/摘要，
    # 任一块解析失败不影响其他块。
    for chunk in re.split(r'(?i)<div[^>]+class="[^"]*\bresult\b', body)[1:]:
        link = re.search(r'(?is)<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk)
        if not link:
            continue
        title = _strip_html(link.group(2))
        if not title:
            continue
        # 百度的摘要 class 名带随机后缀且经常变，靠固定 class 匹配很容易失效。
        # 这里直接把整块转纯文本再剔掉标题，剩下的开头就是摘要。
        plain = _strip_html(chunk.split("</h3>", 1)[-1])
        snippet = re.sub(r"\s+", " ", plain).strip()
        lines.append(f"{len(lines) + 1}. {title}\n   {link.group(1)}\n   {snippet[:300]}")
        if len(lines) >= count:
            break
    if not lines:
        raise RuntimeError("解析百度结果页失败（可能触发了反爬）")
    return "\n".join(lines)


async def _search_duckduckgo(query: str, count: int) -> str:
    """无需 API key 的兜底搜索，抓 DuckDuckGo 的 HTML 版结果页。"""
    status, body = await _fetch(
        "https://html.duckduckgo.com/html/",
        method="POST",
        timeout=25.0,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({"q": query, "kl": "cn-zh"}),
    )
    if status != 200:
        raise RuntimeError(f"DuckDuckGo 返回 HTTP {status}")

    blocks = re.findall(r'(?is)<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body)
    snippets = re.findall(r'(?is)<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', body)
    lines = []
    for i, (href, title) in enumerate(blocks[:count]):
        # DDG 的 HTML 版把真实地址包在 uddg 查询参数里
        real = href
        matched = re.search(r"[?&]uddg=([^&]+)", href)
        if matched:
            real = urllib.parse.unquote(matched.group(1))
        snippet = _strip_html(snippets[i]) if i < len(snippets) else ""
        lines.append(f"{i + 1}. {_strip_html(title)}\n   {real}\n   {snippet[:300]}")
    return "\n".join(lines) if lines else "没有找到相关结果。"


@tool(
    name="web_search",
    description=(
        "联网搜索互联网上的实时信息。当用户问到新闻、时事、最新版本、股价、比赛结果、"
        "某个你不确定或知识截止后才出现的事物时使用。返回若干条标题、链接和摘要。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，用简洁的自然语言，不要加引号或搜索语法"},
            "count": {"type": "integer", "description": "返回结果条数，1 到 10，默认 5"},
        },
        "required": ["query"],
    },
    level="user",
    untrusted_output=True,
    timeout=30.0,
)
async def _tool_web_search(args: dict, ctx: AgentContext) -> str:
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "error: query 不能为空"
    try:
        count = max(1, min(int(args.get("count") or 5), 10))
    except (TypeError, ValueError):
        count = 5

    provider = str(_setting("Agent.search.provider", "auto") or "auto").strip().lower()
    tavily_key = str(_setting("Agent.search.tavily_api_key", "") or "").strip()
    bocha_key = str(_setting("Agent.search.bocha_api_key", "") or "").strip()

    # auto：有 key 就优先用付费源（结果质量高且稳定），否则回落到免费源。
    # 免费源里百度放在 DuckDuckGo 前面：国内网络下 DDG 常直接连不上。
    free = [("baidu", ""), ("duckduckgo", "")]
    if provider == "auto":
        order = ([("tavily", tavily_key)] if tavily_key else []) + \
                ([("bocha", bocha_key)] if bocha_key else []) + free
    elif provider == "tavily":
        order = [("tavily", tavily_key)] + free
    elif provider == "bocha":
        order = [("bocha", bocha_key)] + free
    elif provider == "duckduckgo":
        order = [("duckduckgo", ""), ("baidu", "")]
    else:
        order = free

    errors = []
    for name, key in order:
        if name in ("tavily", "bocha") and not key:
            errors.append(f"{name}: 未配置 API Key")
            continue
        try:
            ctx.say(f"web_search[{name}] {query}", "AGENT")
            if name == "tavily":
                return await _search_tavily(query, count, key)
            if name == "bocha":
                return await _search_bocha(query, count, key)
            if name == "baidu":
                return await _search_baidu(query, count)
            return await _search_duckduckgo(query, count)
        except Exception as e:
            errors.append(f"{name}: {e}")
    return "error: 所有搜索源都失败了 —— " + "；".join(errors)


# ==================== 抓取网页 ====================

@tool(
    name="fetch_webpage",
    description=(
        "打开一个网页链接并读取它的正文内容。当用户直接给出链接要你看，"
        "或 web_search 的摘要不足以回答问题、需要进一步读原文时使用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要读取的完整网页链接，必须以 http:// 或 https:// 开头"},
        },
        "required": ["url"],
    },
    level="user",
    untrusted_output=True,
    timeout=30.0,
)
async def _tool_fetch_webpage(args: dict, ctx: AgentContext) -> str:
    url = str(args.get("url", "") or "").strip()
    if not url:
        return "error: url 不能为空"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "error: 只支持 http/https 链接"
    host = (parsed.hostname or "").lower()
    # 明确点名的元数据服务先挡掉，其余交给 DNS 解析后的网段校验
    if host in ("metadata.google.internal", "metadata") or host.endswith((".local", ".internal")):
        return "error: 出于安全限制，不能访问内网或本机地址。"
    _, host_err = resolve_and_check_host(host)
    if host_err:
        return f"error: 出于安全限制，不能访问内网或本机地址（{host_err}）。"

    ctx.say(f"fetch_webpage {url}", "AGENT")
    try:
        # check_host=True：每一跳都重新解析校验，防止 302 引到内网
        status, body = await _fetch(url, timeout=25.0, check_host=True)
    except Exception as e:
        return f"error: 打开网页失败：{e}"
    if status != 200:
        return f"error: 网页返回 HTTP {status}"

    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    title = _strip_html(title_match.group(1)) if title_match else ""
    text = _strip_html(body)
    if not text:
        return "error: 该网页没有可读的文本内容（可能是纯 JS 渲染页面或图片站）。"
    return f"标题：{title}\n链接：{url}\n\n{text}"

