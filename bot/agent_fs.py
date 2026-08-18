# -*- coding: utf-8 -*-
"""Agent 文件与执行工具 —— file_read / write / edit / grep /
execute_shell / execute_python。

XcBot 没有沙箱，file_read 等工具只能操作会话工作区内的文件，这两个工具默认开启但只有管理用户能调用（项目只有普通用户/管理用户两档，
没有更高等级），风险在提示词和 WebUI 描述里都写明了。

文件与执行工具全部是 admin 级，不做目录和文件名限制——管理员本来就能用
execute_shell 读写整台机器，单独把 file_read 关在某个目录里只是自欺欺人。
但敏感文件判断和目录限制本身要保留：send_file / send_image 是 user 级，
普通用户触发时会被限制在会话工作区内（管理员触发则不限制）。
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import threading
import uuid
from pathlib import Path

from bot.agent import AgentContext, tool

# main.py 启动时注入，用于读 Agent.exec_timeout
_settings_reader = None
_base_dir = Path(".").resolve()

# 这些文件名/后缀一律拒绝读写：含 API Key、凭据或会被误改坏的运行时数据
_BLOCKED_NAMES = {
    "config.json", "config.default.json", "id_rsa", "id_ed25519",
    ".env", ".env.local", "credentials.json", "token.json", "tokens.json",
    # MCP 服务器配置可能含远程服务的 headers 认证信息
    "mcp_server.json", "mcp_servers.json",
}
_BLOCKED_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore")
# 目录名一律小写存放，比较前把待查路径也转小写：Windows 路径不区分大小写，
# 直接比较的话 .GIT/config、.Venv/ 都能绕过。
_BLOCKED_PARTS = {"__pycache__", ".git", ".venv", "node_modules", ".idea", ".vscode"}


def _blocked_part_hit(parts) -> bool:
    return any(str(x).casefold() in _BLOCKED_PARTS for x in parts)
# 敏感文件的常见衍生形式。webui_core/json_io.write_json 每次保存配置都会生成
# config.json.20260802120000_123456.bak，内容和 config.json 一样含完整 API Key；
# 只比对文件名的话这类备份会被放行。
_BLOCKED_STEMS = {
    "config", "credentials", "credential", "token", "tokens",
    "secret", "secrets", "password", "passwords",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "config_backup", "mcp_server", "keystore", "htpasswd",
    # 云服务与 API 密钥常用文件名
    "api_key", "api_keys", "apikey", "apikeys",
    "service-account", "service_account", "serviceaccount",
    "auth", "authorization", "oauth", "refresh_token", "access_token",
}
# 整个文件名就命中即拒。凭据类文件多半没有扩展名，靠 stem 规则匹配不到。
_BLOCKED_EXACT = {
    "credentials", "credential", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".gitconfig",
    ".htpasswd", ".dockercfg", ".pgpass", "known_hosts", "authorized_keys",
}
# 位于这些目录之下的任何文件都拒。凭据目录里的文件名千奇百怪，逐个列不完。
_BLOCKED_DIRS = {
    ".ssh", ".aws", ".kube", ".gnupg", ".docker", ".azure", ".gcloud",
    ".config/gcloud", ".password-store",
}
_STRIPPABLE_SUFFIXES = (
    ".bak", ".backup", ".old", ".orig", ".tmp", ".temp", ".swp", ".save",
    ".copy", ".原件", "~",
)


def _is_sensitive_path(path) -> bool:
    """路径级敏感判断：命中敏感目录，或文件名本身敏感。

    直接读取和递归搜索必须共用同一套判断，否则 file_read 拒绝的东西
    grep 一下就漏出来了。
    """
    try:
        parts = [str(p).casefold() for p in Path(path).parts]
    except Exception:
        return True          # 解析不了就当敏感，宁可拒绝
    for i, part in enumerate(parts):
        if part in _BLOCKED_DIRS:
            return True
        if part in _BLOCKED_PARTS:
            return True
        # 处理 .config/gcloud 这种两级目录
        if i + 1 < len(parts) and f"{part}/{parts[i + 1]}" in _BLOCKED_DIRS:
            return True
    return _is_sensitive_name(parts[-1] if parts else "")


def _is_sensitive_name(name: str) -> bool:
    """判断文件名是否敏感。会剥掉备份/临时后缀和时间戳再比对。

    例：config.json.20260802120000_123456.bak → config.json → 命中黑名单。
    """
    lowered = str(name or "").lower()
    if lowered in _BLOCKED_NAMES or lowered in _BLOCKED_EXACT:
        return True
    if lowered.endswith(_BLOCKED_SUFFIXES):
        return True

    # 反复剥掉尾部的备份后缀与纯数字/时间戳段，直到不再变化
    probe = lowered
    for _ in range(8):
        before = probe
        for suffix in _STRIPPABLE_SUFFIXES:
            if probe.endswith(suffix):
                probe = probe[: -len(suffix)]
                break
        # 时间戳段形如 .20260802120000_123456 或 .1 .2
        probe = re.sub(r"\.\d[\d_-]*$", "", probe)
        if probe == before:
            break
    if probe != lowered and (probe in _BLOCKED_NAMES or probe in _BLOCKED_EXACT
                             or probe.endswith(_BLOCKED_SUFFIXES)):
        return True

    # 兜底：主干名命中且带 json/env/pem 之类的敏感段，如 config.prod.json
    stem = probe.split(".", 1)[0]
    if stem in _BLOCKED_STEMS and probe != stem:
        return True
    if ".env" in probe:
        return True
    return False


# 按规范化路径加锁。file_write / file_edit 都是「读-改-写」，并行工具调用下
# 两个 edit 打同一个文件时后写者会覆盖先写者，原子替换只防半截文件、
# 防不了逻辑覆盖。锁按路径缓存，数量等于被改过的文件数，无需清理。
_path_locks: "dict[str, threading.Lock]" = {}
_path_locks_guard = threading.Lock()


def _has_ambiguous_alternation(pattern: str) -> bool:
    """检测「带量词的分组里有交替分支，且分支之间前缀重叠」。

    (a|aa)+ 不含量词套量词，所以嵌套量词检测放过它；但两个分支能匹配同一段
    输入，回溯路径数随长度指数增长——26 个字符就能跑几百毫秒。
    判据：分组内有 | ，分组后紧跟量词，且任意两个分支存在前缀包含关系
    （(a|aa) 里 "a" 是 "aa" 的前缀）。前缀不重叠的 (foo|bar)+ 是线性的，放行。
    """
    text = str(pattern or "")
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch != "(":
            i += 1
            continue
        # 找配对的闭括号
        depth = 0
        j = i
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(text):
            break
        inner = text[i + 1:j]
        nxt = text[j + 1: j + 2]
        quantified = nxt in ("+", "*") or (nxt == "{" and text[j + 2: j + 3].isdigit())
        if quantified and "|" in inner:
            # 只看顶层的 |
            branches, depth2, buf = [], 0, ""
            k = 0
            while k < len(inner):
                c = inner[k]
                if c == "\\":
                    buf += inner[k:k + 2]
                    k += 2
                    continue
                if c == "(":
                    depth2 += 1
                elif c == ")":
                    depth2 -= 1
                if c == "|" and depth2 == 0:
                    branches.append(buf)
                    buf = ""
                else:
                    buf += c
                k += 1
            branches.append(buf)
            plain = [b for b in branches if b and not any(m in b for m in ".*+?[](){}^$|")]
            for a in plain:
                for b in plain:
                    if a is not b and (a.startswith(b) or b.startswith(a)):
                        return True
        i = j + 1
    return False


def _has_nested_quantifier(pattern: str) -> bool:
    """判断正则里是否存在「量词套量词」。

    只有分组内部已经带量词、分组外又加一个量词时才会指数级回溯，例如
    (a+)+、(x*)* 这类。而 (ab)+c、(foo|bar)+ 这种单层分组加量词是
    线性的、也是最常用的写法，不能一起拦掉。
    """
    text = str(pattern or "")
    depth_stack = []          # 每层分组内是否见过量词
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth_stack.append(False)
        elif ch == ")":
            had_quant = depth_stack.pop() if depth_stack else False
            # 看闭括号后紧跟的是不是量词
            nxt = text[i + 1: i + 2]
            if had_quant and (nxt in ("+", "*") or nxt == "{"):
                return True
        elif ch in ("+", "*") or (ch == "{" and text[i + 1: i + 2].isdigit()):
            if depth_stack:
                depth_stack[-1] = True
        i += 1
    return False


def _atomic_write_text(path, content: str) -> None:
    """先写临时文件再原子替换。直接 write_text 时进程崩在中途会留下截断文件。"""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fp:
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _path_lock(path) -> "threading.Lock":
    # casefold 而不是 lower：与路径敏感判断保持同一套大小写折叠规则
    key = str(path).casefold()
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


MAX_READ_BYTES = 512 * 1024
MAX_WRITE_CHARS = 200_000


def bind(settings_reader, base_dir: str) -> None:
    global _settings_reader, _base_dir
    _settings_reader = settings_reader
    _base_dir = Path(base_dir).resolve()


def _setting(path: str, default=None):
    if callable(_settings_reader):
        try:
            value = _settings_reader(path, default)
            return default if value is None else value
        except Exception:
            return default
    return default


def workspace_root() -> Path:
    """所有会话工作区的父目录。"""
    return (_base_dir / "data" / "agent_workspace").resolve()


def session_key_of(ctx) -> str:
    """会话标识。群聊按群分，私聊按人分，两者不共用命名空间。

    刻意不做回退：拿不到 id 时返回空串，由调用方落到 shared 目录，
    绝不把两个不同会话映射到同一个 key 上。
    """
    try:
        if getattr(ctx, "is_group", False) and getattr(ctx, "group_id", ""):
            return f"group_{str(ctx.group_id).strip()}"
        if getattr(ctx, "user_id", ""):
            return f"private_{str(ctx.user_id).strip()}"
    except Exception:
        pass
    return ""


def session_workspace(ctx) -> Path:
    """当前会话的工作区路径。只计算不创建——目录在真正要写东西时才建。

    id 里只允许数字和下划线：group_id/user_id 理论上都是纯数字，
    但它们来自协议端，一旦掺进 .. 或路径分隔符就能跳出 agent_workspace。
    """
    key = session_key_of(ctx)
    if not key or not re.fullmatch(r"[a-z]+_[0-9_]+", key):
        key = "shared"
    return (workspace_root() / key).resolve()


def ensure_session_workspace(ctx) -> tuple[Path | None, str]:
    """要往工作区里写东西时才调这个，负责真正建目录。"""
    path = session_workspace(ctx)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return None, f"error: 无法创建工作目录 {path}：{e}"
    return path, ""


def clear_session_workspace(is_group: bool, chat_id) -> tuple[int, str]:
    """删掉某个会话的工作区，给 /reset 用。返回 (删掉的文件数, 错误文本)。

    不接 ctx 而是直接收 id：/reset 走的是命令分支，那里没有 AgentContext。
    只删 agent_workspace 下的东西，管理员额外配置的目录一律不碰——
    那些目录里可能有用户自己的资料，清对话记忆不该动它们。
    """
    key = ""
    try:
        text = str(chat_id or "").strip()
        if text:
            key = f"group_{text}" if is_group else f"private_{text}"
    except Exception:
        key = ""
    # 和 session_workspace 用同一套消毒规则，避免 ../ 之类删到别处
    if not key or not re.fullmatch(r"[a-z]+_[0-9_]+", key):
        return 0, ""
    target = (workspace_root() / key).resolve()
    # 二次确认：resolve 之后必须仍在 agent_workspace 里面
    root = workspace_root()
    if target == root or root not in target.parents:
        return 0, ""
    if not target.exists():
        return 0, ""
    try:
        count = sum(1 for p in target.rglob("*") if p.is_file())
    except Exception:
        count = 0
    try:
        import shutil
        shutil.rmtree(target)
    except Exception as e:
        return 0, str(e)
    return count, ""


def primary_root(ctx=None) -> str:
    """当前会话的工作目录，用于告知模型默认在哪干活。

    这只是个默认落脚点：文件与执行类工具不做目录限制，模型给绝对路径
    就能访问别处。写成一个真实存在、相对路径也指向它的目录，
    模型才不会瞎猜。
    """
    try:
        return str(session_workspace(ctx))
    except Exception:
        return ""


def _resolve_path(raw: str, ctx=None, *, unrestricted: bool = False,
                  workspace_only: bool = False) -> tuple[Path | None, str]:
    """把模型给的路径解析成绝对路径。

    相对路径的基准是当前会话的工作区——模型说「写 report.md」时应该落在
    自己的地盘里。但工作区只是默认落脚点，不是围栏。

    unrestricted=True 时不做任何目录和文件名限制。五个文件工具和两个执行
    工具都传这个：它们全是 admin 级，而 execute_shell 本来就能 `cat 任意路径`，
    单独把 file_read 关在某个目录里只是自欺欺人。用户让 AI 改自己项目的
    代码、看某个日志，不该先去配置里加一遍目录。

    workspace_only=True 时严格限制在会话工作区内，且保留敏感文件屏蔽。
    send_file / send_image 这类 user 级工具必须用它——否则群里任何人都能
    让 AI 把机器上的任意文件发出来。
    """
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return None, "error: path 不能为空"
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = session_workspace(ctx) / candidate
        # resolve() 之后再比较：../ 和符号链接都能穿越出去
        candidate = candidate.resolve()
    except Exception as e:
        return None, f"error: 路径无法解析：{e}"

    if unrestricted:
        return candidate, ""

    if not workspace_only:
        # 理论上到不了这里：非 unrestricted 的调用方只有 workspace_only 一种。
        # 留个 fail-closed 兜底，将来新增调用点忘了传参时按最严格处理。
        workspace_only = True

    root = session_workspace(ctx)
    if not (candidate == root or root in candidate.parents):
        return None, (
            f"error: {candidate} 不在你这次会话的工作目录内。"
            "这个工具只能处理你自己在工作目录里生成的文件。"
        )
    if _is_sensitive_path(candidate):
        return None, f"error: {candidate.name} 属于敏感文件或位于凭据目录中，拒绝访问。"
    if _blocked_part_hit(candidate.parts):
        return None, "error: 该路径位于缓存或版本控制目录中，拒绝访问。"
    return candidate, ""


@tool(
    name="file_read",
    description=(
        "读取一个文本文件的内容。可以用 start_line / end_line 只读其中一段（行号从 1 开始）。"
        "当某个工具的结果被保存到 overflow 文件时，也用这个工具去读全文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，可以是绝对路径或相对机器人目录的路径"},
            "start_line": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
            "end_line": {"type": "integer", "description": "结束行号（含），默认读到文件末尾或 2000 行"},
        },
        "required": ["path"],
    },
    level="admin",
    timeout=30.0,
)
async def _tool_file_read(args: dict, ctx: AgentContext) -> str:
    path, err = _resolve_path(args.get("path"), ctx, unrestricted=True)
    if err:
        return err
    if not path.exists():
        return f"error: 文件不存在：{path}"
    if path.is_dir():
        return f"error: {path} 是目录，请用 file_list 工具列出内容。"
    try:
        start = max(1, int(args.get("start_line") or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        end_arg = int(args.get("end_line") or 0)
    except (TypeError, ValueError):
        end_arg = 0

    try:
        size = path.stat().st_size
        if size <= MAX_READ_BYTES:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        else:
            # 大文件不整读，但也不该直接拒绝——工具结果溢出后正是靠 file_read
            # 按区间取回内容的。这里流式跳到 start，只保留请求的那一段。
            want_end = min(end_arg if end_arg > 0 else start + 1999, start + 1999)
            picked = []
            truncated = False
            with path.open("r", encoding="utf-8", errors="replace") as fp:
                for lineno, text in enumerate(fp, start=1):
                    if lineno < start:
                        continue
                    if lineno > want_end:
                        truncated = True
                        break
                    picked.append(text.rstrip('\n'))
            if not picked:
                return (f"error: start_line={start} 超过文件实际行数，"
                        "请用更小的起始行号重试。")
            last = start + len(picked) - 1
            body = '\n'.join(
                f"{start + i}	{t}" for i, t in enumerate(picked)
            )
            ctx.say(f"file_read {path} [{start}:{last}] (流式)", "AGENT")
            tail = "，后面还有内容" if truncated else ""
            return (f"文件：{path}\n文件较大（{size} 字节），"
                    f"已流式读取 {start}-{last} 行{tail}\n\n{body}")
    except Exception as e:
        return f"error: 读取失败：{e}"

    # 空文件和越界起始行必须先挡住：下面按 range(start, end+1) 取 lines[i-1]，
    # 而 max(start, ...) 会把 end 抬到至少等于 start，于是空文件（len=0）或
    # start 超过总行数时都会越界抛 IndexError。
    if not lines:
        ctx.say(f"file_read {path} (空文件)", "AGENT")
        return f"文件：{path}\n总行数：0（空文件）"
    if start > len(lines):
        return (f"error: start_line={start} 超过文件总行数 {len(lines)}，"
                f"请指定 1 到 {len(lines)} 之间的行号。")

    end = end_arg or min(len(lines), start + 1999)
    end = max(start, min(end, len(lines), start + 1999))

    ctx.say(f"file_read {path} [{start}:{end}]", "AGENT")
    body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))
    return f"文件：{path}\n总行数：{len(lines)}，本次显示 {start}-{end} 行\n\n{body}"


@tool(
    name="file_list",
    description="列出一个目录下的文件和子目录，含大小和修改时间。",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "目录路径"}},
        "required": ["path"],
    },
    level="admin",
    timeout=20.0,
)
async def _tool_file_list(args: dict, ctx: AgentContext) -> str:
    path, err = _resolve_path(args.get("path"), ctx, unrestricted=True)
    if err:
        return err
    if not path.exists():
        # 工作区是懒创建的：没建过就说明这个会话还没产出任何文件，
        # 这不是错误，别让模型以为路径写错了反复重试
        if path == session_workspace(ctx):
            return f"目录：{path}\n（空目录，本次会话还没有创建过任何文件）"
        return f"error: 目录不存在：{path}"
    if not path.is_dir():
        return f"error: {path} 不是目录"
    import datetime as _dt
    rows = []
    try:
        for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            try:
                stat = item.stat()
                when = _dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                rows.append(f"{'[目录]' if item.is_dir() else '     '} {item.name}"
                            f"{'' if item.is_dir() else f'  {stat.st_size} 字节'}  {when}")
            except Exception:
                rows.append(f"      {item.name}  (无法读取属性)")
            if len(rows) >= 300:
                rows.append("...（超过 300 项，已截断）")
                break
    except Exception as e:
        return f"error: 列目录失败：{e}"
    ctx.say(f"file_list {path}", "AGENT")
    return f"目录：{path}\n共 {len(rows)} 项\n\n" + ("\n".join(rows) if rows else "（空目录）")


@tool(
    name="file_write",
    description=(
        "创建一个新文件或完全覆盖已有文件的内容。会真实写入磁盘，覆盖前请确认用户是这个意图。"
        "只想改文件中一部分内容时，用 file_edit 更安全。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要写入的文件路径"},
            "content": {"type": "string", "description": "文件的完整新内容"},
        },
        "required": ["path", "content"],
    },
    side_effect=True,
    level="admin",
    timeout=30.0,
)
async def _tool_file_write(args: dict, ctx: AgentContext) -> str:
    path, err = _resolve_path(args.get("path"), ctx, unrestricted=True)
    if err:
        return err
    content = str(args.get("content", "") or "")
    if len(content) > MAX_WRITE_CHARS:
        return f"error: 内容过长（{len(content)} 字，上限 {MAX_WRITE_CHARS}）"
    existed = path.exists()
    if existed and path.is_dir():
        return f"error: {path} 是目录，不能当文件写"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 与 file_edit 共用同一把路径锁：两者并发打同一个文件时，
        # 覆盖写和读改写会互相踩
        with _path_lock(path):
            _atomic_write_text(path, content)
    except Exception as e:
        return f"error: 写入失败：{e}"
    ctx.say(f"file_write {path} ({len(content)} 字)", "AGENT")
    return f"已{'覆盖' if existed else '创建'}文件 {path}，写入 {len(content)} 字、{content.count(chr(10)) + 1} 行。"


@tool(
    name="file_edit",
    description=(
        "在文件里把一段精确匹配的文本替换成新文本。old_string 必须与文件内容完全一致（含缩进）。"
        "如果 old_string 在文件中出现多次，默认会拒绝——请提供更多上下文让它唯一，或设 replace_all=true。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要编辑的文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文，必须精确匹配"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "是否替换所有出现处，默认 false"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    side_effect=True,
    level="admin",
    timeout=30.0,
)
async def _tool_file_edit(args: dict, ctx: AgentContext) -> str:
    path, err = _resolve_path(args.get("path"), ctx, unrestricted=True)
    if err:
        return err
    if not path.exists() or path.is_dir():
        return f"error: 文件不存在或不是普通文件：{path}"
    old = str(args.get("old_string", "") or "")
    new = str(args.get("new_string", "") or "")
    if not old:
        return "error: old_string 不能为空（要创建新文件请用 file_write）"
    if old == new:
        return "error: old_string 与 new_string 相同，无需修改"

    # 参数校验放在读文件之前：不合法就直接返回，不必占着文件锁
    # 不能直接 bool()：模型或兼容层传字符串 "false" 时 bool("false") 是 True，
    # 语义正好反过来，会把整个文件里的所有匹配都替换掉
    raw_all = args.get("replace_all", False)
    if isinstance(raw_all, str):
        low = raw_all.strip().lower()
        if low in ("true", "1", "yes"):
            replace_all = True
        elif low in ("false", "0", "no", ""):
            replace_all = False
        else:
            return f"error: replace_all 只接受布尔值，收到「{raw_all}」"
    elif isinstance(raw_all, bool):
        replace_all = raw_all
    elif isinstance(raw_all, (int, float)):
        replace_all = bool(raw_all)
    else:
        return "error: replace_all 只接受布尔值"

    # 读-改-写整段持锁：并行工具调用下两个 edit 打同一文件时，
    # 各自读一份旧内容再各写一次，后写的会把前一次的修改抹掉
    with _path_lock(path):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"error: 读取失败：{e}"

        count = content.count(old)
        if count == 0:
            return (
                "error: 在文件里找不到 old_string。注意必须精确匹配，包括缩进和换行。"
                "建议先用 file_read 看一下确切内容再重试。"
            )
        if count > 1 and not replace_all:
            return (
                f"error: old_string 在文件里出现了 {count} 次，无法确定改哪一处。"
                "请补充更多上下文让它唯一，或明确设置 replace_all=true。"
            )
        try:
            _atomic_write_text(
                path,
                content.replace(old, new) if replace_all else content.replace(old, new, 1),
            )
        except Exception as e:
            return f"error: 写入失败：{e}"

    ctx.say(f"file_edit {path} ({count} 处)", "AGENT")
    return f"已修改 {path}，替换了 {count if replace_all else 1} 处。"


@tool(
    name="grep_files",
    description="在目录下按正则表达式搜索文本，返回命中的文件、行号和该行内容。用于在代码或日志里定位内容。",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式"},
            "path": {"type": "string", "description": "要搜索的目录或文件路径"},
            "glob": {"type": "string", "description": "文件名过滤，如 *.py、*.log。默认所有文本文件"},
            "max_results": {"type": "integer", "description": "最多返回多少条匹配，默认 50，上限 200"},
        },
        "required": ["pattern", "path"],
    },
    level="admin",
    timeout=60.0,
    # 遍历目录 + 逐行正则都是同步阻塞的：灾难性回溯正则（如 (a+)+b）
    # 会把事件循环卡死，超时与 /停止 全部失效
    cpu_bound=True,
)
def _tool_grep_files(args: dict, ctx: AgentContext) -> str:
    path, err = _resolve_path(args.get("path"), ctx, unrestricted=True)
    if err:
        return err
    if not path.exists():
        return f"error: 路径不存在：{path}"
    try:
        raw_pattern = str(args.get("pattern", "") or "")
        # 正则本身先做复杂度检查。Python 的 re 没有回溯上限，
        # (a+)+$ 这类模式在稍长的行上就会指数级回溯；而它是同步执行的，
        # 放在线程池里也只能让调用方超时返回，线程本身还在烧 CPU。
        # 拒绝明显的嵌套量词是唯一能在事前挡住的办法。
        if len(raw_pattern) > 200:
            return "error: 正则表达式过长（上限 200 字符）"
        if _has_nested_quantifier(raw_pattern):
            return (
                "error: 这个正则含嵌套量词（例如 (a+)+、(x*)* ），"
                "在长文本上可能引发指数级回溯把机器人卡住，已拒绝。"
                "请改写成不嵌套的形式，或换更精确的关键词。"
            )
        if _has_ambiguous_alternation(raw_pattern):
            return (
                "error: 这个正则里带量词的分组含有前缀重叠的分支"
                "（例如 (a|aa)+ ），同样会引发指数级回溯，已拒绝。"
                "请让各分支互不重叠，或换更精确的关键词。"
            )
        regex = re.compile(raw_pattern)
    except re.error as e:
        return f"error: 正则表达式不合法：{e}"
    try:
        limit = max(1, min(int(args.get("max_results") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    pattern_glob = str(args.get("glob", "") or "").strip() or "*"

    if path.is_file():
        files = [path]
    else:
        files = []
        for p in path.rglob(pattern_glob):
            try:
                if not p.is_file():
                    continue
                real = p.resolve()
            except (OSError, RuntimeError):
                continue
            files.append(real)

    ctx.say(f"grep_files {regex.pattern} in {path} ({len(files)} 个文件)", "AGENT")
    hits, scanned = [], 0
    for target in files[:3000]:
        try:
            if target.stat().st_size > MAX_READ_BYTES:
                continue
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), start=1):
            # 超长单行直接跳过：回溯代价随行长指数增长，
            # 一行几十 KB 的压缩过的 JS/JSON 是最容易触发的场景
            if len(line) > 4000:
                continue
            if regex.search(line):
                hits.append(f"{target}:{lineno}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break
    if not hits:
        return f"在 {scanned} 个文件里没有找到匹配「{regex.pattern}」的内容。"
    return f"扫描 {scanned} 个文件，找到 {len(hits)} 条匹配：\n\n" + "\n".join(hits)


def _exec_timeout() -> float:
    try:
        return max(5.0, min(float(_setting("Agent.exec_timeout", 60) or 60), 600.0))
    except (TypeError, ValueError):
        return 60.0


def _format_exec_output(code: int, stdout: str, stderr: str, timed_out: bool, limit: int = 6000) -> str:
    parts = [f"退出码：{code}" if not timed_out else "执行超时，进程已被杀掉"]
    if stdout.strip():
        parts.append(f"--- 标准输出 ---\n{stdout.strip()[:limit]}")
    if stderr.strip():
        parts.append(f"--- 错误输出 ---\n{stderr.strip()[:limit]}")
    if not stdout.strip() and not stderr.strip():
        parts.append("（没有任何输出）")
    return "\n".join(parts)


def _kill_process_tree(proc) -> None:
    """杀掉子进程及其所有后代。

    proc.kill() 只杀直接子进程：shell 里 fork 出来的、或 python 自己起的子进程
    会变成孤儿继续跑。用 psutil 遍历进程树逐个杀；psutil 不可用时退回
    taskkill /T（Windows）或进程组信号（POSIX）。
    """
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        import psutil
        parent = psutil.Process(pid)
        victims = parent.children(recursive=True) + [parent]
        for p in victims:
            try:
                p.kill()
            except Exception:
                pass
        psutil.wait_procs(victims, timeout=3)
        return
    except Exception:
        pass
    # 没有 psutil 时的兜底
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def _run_subprocess(argv: list[str] | str, shell: bool, cwd: Path) -> tuple[int, str, str, bool]:
    """跑子进程并强制超时。超时或被取消时杀掉整个进程树，不留孤儿。"""
    timeout = _exec_timeout()
    creation = {}
    if sys.platform == "win32":
        # 让 kill 能带走整个进程组，否则 shell 里 fork 出的子进程会活下来
        creation["creationflags"] = getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        # POSIX：单独设进程组，才能用 killpg 一次带走全部后代
        creation["start_new_session"] = True
    if shell:
        proc = await asyncio.create_subprocess_shell(
            argv if isinstance(argv, str) else " ".join(argv),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd), **creation,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd), **creation,
        )
    try:
        out, errout = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode("utf-8", "replace"), errout.decode("utf-8", "replace"), False
    except asyncio.TimeoutError:
        _kill_process_tree(proc)
        return -1, "", f"执行超过 {timeout:.0f} 秒被强制终止", True
    except asyncio.CancelledError:
        # /停止 取消了等待这个协程的 task。不处理的话协程就地退出、
        # 子进程失去回收者继续在后台跑到天荒地老。
        _kill_process_tree(proc)
        raise


@tool(
    name="execute_python",
    description=(
        "在一个独立的 Python 子进程里执行代码，返回它的标准输出。适合做复杂计算、数据处理、格式转换。"
        "代码里请用 print() 输出结果，否则你看不到返回值。每次执行都是全新进程，变量不会保留。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码，用 print 输出结果"},
        },
        "required": ["code"],
    },
    side_effect=True,
    level="admin",
    timeout=600.0,
)
async def _tool_execute_python(args: dict, ctx: AgentContext) -> str:
    code = str(args.get("code", "") or "").strip()
    if not code:
        return "error: code 不能为空"
    if len(code) > 20000:
        return "error: 代码过长（上限 20000 字）"

    workdir, ws_err = ensure_session_workspace(ctx)
    if ws_err:
        return ws_err

    ctx.say(f"execute_python ({len(code)} 字)", "AGENT")
    try:
        # 用 -c 传代码，避免落临时文件；-I 隔离用户 site-packages 与环境变量干扰
        code_ret, out, errout, timed_out = await _run_subprocess(
            # -E 忽略 PYTHONPATH 等环境变量，-P 把 cwd 从 sys.path[0] 去掉。
            # 少了 -P，AI 可以先用 file_write 往工作目录丢一个 json.py，
            # 再 execute_python 里 import json 就会加载那个文件（模块劫持）。
            # 两个 flag 都不影响 site-packages，三方库照常可用。
            [sys.executable, "-E", "-P", "-X", "utf8", "-c", code], shell=False, cwd=workdir
        )
    except Exception as e:
        return f"error: 启动 Python 子进程失败：{e}"
    return _format_exec_output(code_ret, out, errout, timed_out)


@tool(
    name="execute_shell",
    description=(
        "执行一条系统命令并返回输出。适合查看系统状态、处理文件、调用外部程序。"
        "命令在机器人所在机器上以机器人的权限运行，属于高风险操作——执行前应确认用户真的需要。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的完整命令行"},
        },
        "required": ["command"],
    },
    side_effect=True,
    level="admin",
    timeout=600.0,
)
async def _tool_execute_shell(args: dict, ctx: AgentContext) -> str:
    command = str(args.get("command", "") or "").strip()
    if not command:
        return "error: command 不能为空"
    if len(command) > 4000:
        return "error: 命令过长（上限 4000 字）"

    workdir, ws_err = ensure_session_workspace(ctx)
    if ws_err:
        return ws_err

    ctx.say(f"execute_shell {command[:120]}", "AGENT")
    try:
        code, out, errout, timed_out = await _run_subprocess(command, shell=True, cwd=workdir)
    except Exception as e:
        return f"error: 启动子进程失败：{e}"
    return _format_exec_output(code, out, errout, timed_out)
