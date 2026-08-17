# -*- coding: utf-8 -*-
"""XcBot lightweight WebUI.

只使用 Python 标准库，避免给机器人增加额外依赖。提供：
- config.json / 插件配置的读取与保存
- 运行状态、启动参数、环境信息
- stdout/stderr 实时日志缓冲与最近日志文件读取
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import html
import io
import json
import os
import platform
import base64
import shutil
import subprocess
import sys
import atexit
import tempfile
import threading
import time
import traceback
import weakref
import urllib.parse
import urllib.request
import urllib.error
import uuid
import zipfile
import re
import gc
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from bot import knowledge_base as _knowledge_base


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "data" / "webui"

def _log_file(date: Optional[str] = None) -> Path:
    """返回业务日（凌晨 4 点切换，或指定日期）的日志文件路径。"""
    d = date or (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d")
    return LOG_DIR / f"runtime-{d}.log"

# ponytail: 保留 LOG_FILE 供旧引用（路径仅用于展示，实际写入用 _log_file()）
LOG_FILE = LOG_DIR / "runtime.log"
BOT_ICON_PATH = BASE_DIR / "assets" / "icon.jpg"
# 用于在线检查/拉取更新的 GitHub 仓库（owner/repo），可在 config.json 的 Others.github_repo 中覆盖。
GITHUB_REPO = os.environ.get("XCBOT_GITHUB_REPO", "Qzy327422/XcBot")
# 插件商店仓库（owner/repo），存放 registry.json 与 plugins/ 目录
PLUGIN_STORE_REPO = os.environ.get("XCBOT_PLUGIN_STORE_REPO", "Qzy327422/XcBot-Plugins")
PLUGIN_DIR = BASE_DIR / "plugins"
LEGACY_CONFIG_PATHS = [
    BASE_DIR / "Manage_User.ini",
    BASE_DIR / "Super_User.ini",
    BASE_DIR / "blacklist.sr",
    BASE_DIR / "plugins" / "split_reply_quote.json",
]

_server: Optional[ThreadingHTTPServer] = None
_server_thread: Optional[threading.Thread] = None
_STATIC_ASSET_CONTENT: Dict[str, str] = {}
_STATIC_ASSET_CONTENT: Dict[str, str] = {}
_started_at = time.time()
# 实时日志页需要覆盖足够长的排障窗口。这里只保存轻量行对象；完整日志仍按天落盘。
_log_buffer = deque(maxlen=3000)
_log_lock = threading.RLock()
_capture_installed = False
_capture_stdout = None
_capture_stderr = None
_config_saved_callback = None
_pre_restart_callback = None  # 由外部注册：自动更新重启前保存状态、释放资源等
_qq_send_callback = None  # 由 main.py 注册：通过当前 OneBot / Hyper 连接发送 QQ 消息
_debug_self_message_callback = None  # 由 main.py 注册：给机器人自己发调试私聊消息
_chatroom_agent_callback = None  # 由 main.py 注册：复用统一 Agent / 上下文 / 追踪链路
_chatroom_stop_callback = None  # 由 main.py 注册：中断聊天室 Agent 循环
_webui_reconfigure_lock = threading.RLock()
_update_cache_lock = threading.RLock()
_update_cache = {"timestamp": 0.0, "data": None}
_statistics_cache: Dict[str, Any] = {"timestamp": 0.0, "data": None}
_statistics_cache_lock = threading.Lock()
_stat_inc_lock = threading.Lock()
_stat_inc_state: Dict[str, Any] = {
    "log_path": "",
    "file_size": 0,
    "last_check": 0,
    "total_messages": 0,
    "message_trend": None,
    "message_scene": None,
    "api_request_history": None,
    "model_rank": None,
    "session_tokens_1d": None,
    "token_trend_1d": None,
    "pending_api_by_scene": None,
}
_UPDATE_UNKNOWN = {"status": "unknown", "message": "未检查", "has_update": False}
_update_install_lock = threading.Lock()
_update_install_status = {
    "state": "idle",
    "text": "未检查",
    "detail": "",
    "updated_at": int(time.time()),
}
_connection_status = {
    "state": "starting",
    "text": "正在启动",
    "detail": "等待 OneBot / Hyper 连接",
    "updated_at": int(time.time()),
}
from webui_core.resources import format_uptime as _format_uptime
from webui_core.resources import get_resource_usage as _get_resource_usage

from webui_core.features import DEFAULT_FEATURE_SWITCHES, FEATURE_META
from webui_core.agent_meta import (
    AGENT_TOOL_META, AGENT_TOOL_GROUPS,
    visible_tools, card_members, configurable_keys,
)

class TeeStream(io.TextIOBase):
    """将 stdout/stderr 同步写到原始流、内存缓冲和日志文件。"""

    def __init__(self, original, stream_name: str):
        self.original = original
        self.stream_name = stream_name
        self._encoding = getattr(original, "encoding", "utf-8") or "utf-8"
        self._errors = getattr(original, "errors", "replace") or "replace"

    @property
    def encoding(self):
        return self._encoding

    @property
    def errors(self):
        return self._errors

    def writable(self):
        return True

    def isatty(self):
        return getattr(self.original, "isatty", lambda: False)()

    def fileno(self):
        return self.original.fileno()

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        try:
            self.original.write(s)
            self.original.flush()
        except Exception:
            pass
        _append_log(s, self.stream_name)
        return len(s)


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHF]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]')

def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)

def _append_log(text: str, stream_name: str = "stdout"):
    if text == "":
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = text.splitlines()
    if text.endswith(("\n", "\r")) and lines:
        pass
    elif not lines:
        lines = [text]
    log_path = _log_file()
    with _log_lock:
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            for line in lines:
                item = {"time": now, "stream": stream_name, "message": line}  # buffer保留ANSI供前端着色
                _log_buffer.append(item)
                f.write(f"[{now}] [{stream_name}] {_strip_ansi(line)}\n")  # 文件剥离ANSI


def install_log_capture():
    global _capture_installed, _capture_stdout, _capture_stderr
    if _capture_installed:
        return
    _capture_stdout = TeeStream(sys.stdout, "stdout")
    _capture_stderr = TeeStream(sys.stderr, "stderr")
    sys.stdout = _capture_stdout
    sys.stderr = _capture_stderr
    _capture_installed = True


from webui_core.json_io import atomic_write_text as _atomic_write_text
from webui_core.json_io import read_json, write_json
from webui_core.json_io import config_transaction

def cleanup_legacy_config_files():
    """删除历史遗留的外部配置文件，强制统一只保留 config.json。

    修复 #7：原实现无条件 unlink 历史文件，且 start_webui 启动时就会调用。
    若用户从老版本升级、还没把数据迁进 config.json，旧名单/黑名单可能直接丢失。
    这里改为：① 仅当 config.json 已存在时才清理（说明已切换到新配置体系）；
    ② 删除前先把内容拷贝到 data/legacy_backup/ 留存，便于事后追回。
    """
    if not CONFIG_PATH.exists():
        return
    backup_root = BASE_DIR / "data" / "legacy_backup"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for legacy_path in LEGACY_CONFIG_PATHS:
        try:
            if not legacy_path.exists():
                continue
            try:
                backup_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(
                    legacy_path,
                    backup_root / f"{legacy_path.name}.{timestamp}.bak",
                )
            except Exception as backup_error:
                # 备份失败时放弃删除，避免无声丢数据。
                print(f"清理旧配置 {legacy_path} 前备份失败，保留原文件：{backup_error}")
                continue
            legacy_path.unlink()
        except Exception:
            pass


def normalize_bool_config(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "启用", "开启", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "禁用", "关闭", "否"}:
        return False
    return bool(default)


from bot.llm_config import build_llm_endpoints_from_providers
from bot.llm_config import force_apply_llm_endpoints_from_config as _force_apply_llm_endpoints_core
from bot.llm_config import normalize_legacy_endpoints as _normalize_webui_llm_endpoints
from bot.llm_config import normalize_llm_providers_config
from bot.llm_config import normalize_provider_keys as _normalize_provider_keys
from bot.llm_config import provider_display_model as _provider_display_model
from bot.llm_config import sync_provider_config

def sync_personality_presets(others: Dict[str, Any]) -> None:
    prompt = str(others.get("personality_prompt", "") or "")
    presets = others.get("personality_presets", [])
    if not isinstance(presets, list) or not presets:
        presets = [{"id": "default", "name": "默认", "prompt": prompt}]
    normalized = []
    seen = set()
    for item in presets:
        if not isinstance(item, dict):
            continue
        pid = re.sub(r"[^a-zA-Z0-9_-]", "", str(item.get("id", "") or "").strip()) or f"preset{len(normalized)+1}"
        if pid in seen:
            pid = f"{pid}_{len(normalized)+1}"
        seen.add(pid)
        normalized.append({
            "id": pid,
            "name": str(item.get("name", "") or pid).strip() or pid,
            "prompt": str(item.get("prompt", "") or ""),
        })
    active = str(others.get("active_personality_preset", "") or "").strip()
    if active not in {x["id"] for x in normalized}:
        active = normalized[0]["id"] if normalized else "default"
    current = next((x for x in normalized if x["id"] == active), None)
    if current:
        others["personality_prompt"] = current.get("prompt", "")
    others["personality_presets"] = normalized
    others["active_personality_preset"] = active


def force_apply_llm_endpoints_from_config(cfg: Dict[str, Any]):
    """WebUI 保存后直接刷新 key_manager，兜底保证 LLM 接口列表无需重启。"""
    try:
        from key_manager import key_manager
        endpoints = _force_apply_llm_endpoints_core(cfg, set_endpoints=key_manager.set_endpoints)
        print(
            f"✅ WebUI 已直接热刷新 LLM 模型轮换: models={len(endpoints)}, "
            f"keys={len(key_manager.get_all_keys())}, current={key_manager.get_current_display()}"
        )
    except Exception as e:
        print(f"WebUI 直接热刷新 LLM 接口列表失败: {e}")


# ==================== 聊天室（WebUI 内置 AI 对话，独立沙盒） ====================
CHATROOM_DIR = BASE_DIR / "data" / "webui" / "chatroom"
_chatroom_lock = threading.RLock()
# 会话 ID 可以无限创建；强引用锁字典会在删除会话后持续泄漏。活动请求持有返回值的
# 强引用即可，空闲锁用弱引用自动回收。
_chatroom_session_locks = weakref.WeakValueDictionary()


def _chatroom_session_lock(session_id: str) -> threading.RLock:
    """同一会话整轮请求串行，不同会话仍可并发。"""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(session_id or ""))
    if not safe:
        raise ValueError("无效的会话 ID")
    with _chatroom_lock:
        lock = _chatroom_session_locks.get(safe)
        if lock is None:
            lock = threading.RLock()
            _chatroom_session_locks[safe] = lock
        return lock


CHATROOM_COMMAND_HINT = "⚠️ 该命令依赖 QQ 群聊/私聊环境，聊天室场景下不可用。"


def _chatroom_others() -> Dict[str, Any]:
    cfg = read_json(CONFIG_PATH, {})
    others = cfg.get("Others", {}) if isinstance(cfg, dict) else {}
    return others if isinstance(others, dict) else {}


def _chatroom_reminder() -> str:
    return str(_chatroom_others().get("reminder", "/") or "/")


def _chatroom_system_prompt() -> str:
    prompt = str(_chatroom_others().get("personality_prompt", "") or "").strip()
    return prompt or "你是一个乐于助人的 AI 助手。"


def _chatroom_models() -> list[Dict[str, Any]]:
    """从 provider/model 轮换配置读取可用模型，不含 key。"""
    endpoints = build_llm_endpoints_from_providers(_chatroom_others())
    if not endpoints:
        endpoints = _normalize_webui_llm_endpoints(_chatroom_others().get("llm_endpoints", []))
    seen = set()
    models = []
    for ep in endpoints:
        display_model = ep.get("display_model") or _provider_display_model(ep.get("provider_id", ""), ep.get("model", ""))
        if not display_model or display_model in seen:
            continue
        seen.add(display_model)
        models.append({
            "model": display_model,
            "raw_model": ep.get("model", ""),
            "base_url": ep.get("base_url", ""),
            "supports_multimodal": bool(ep.get("supports_multimodal", False)),
        })
    return models


def _chatroom_endpoint_for_model(model: str) -> Optional[Dict[str, Any]]:
    """返回首个匹配显示模型或真实模型的 endpoint（含 keys），用于实际调用。"""
    endpoints = build_llm_endpoints_from_providers(_chatroom_others())
    if not endpoints:
        endpoints = _normalize_webui_llm_endpoints(_chatroom_others().get("llm_endpoints", []))
    for ep in endpoints:
        display_model = ep.get("display_model") or _provider_display_model(ep.get("provider_id", ""), ep.get("model", ""))
        if model in {display_model, ep.get("model")} and ep.get("keys"):
            return ep
    return None


def _chatroom_rotation_endpoints(model: str) -> list[Dict[str, Any]]:
    """按轮换顺序返回聊天室可尝试的 endpoint；优先从当前选择模型开始。"""
    endpoints = build_llm_endpoints_from_providers(_chatroom_others())
    if not endpoints:
        endpoints = _normalize_webui_llm_endpoints(_chatroom_others().get("llm_endpoints", []))
    endpoints = [ep for ep in endpoints if ep.get("keys")]
    if not endpoints:
        return []
    selected = str(model or "").strip()
    if not selected:
        return endpoints
    start = None
    for idx, ep in enumerate(endpoints):
        display_model = ep.get("display_model") or _provider_display_model(ep.get("provider_id", ""), ep.get("model", ""))
        if selected in {display_model, ep.get("model")}:
            start = idx
            break
    if start is None:
        return endpoints
    return endpoints[start:] + endpoints[:start]


def _chatroom_http_error_message(e: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        detail = e.read().decode("utf-8", errors="replace")[:300]
    except Exception:
        pass
    return f"模型接口返回错误 {e.code}：{detail or e.reason}"


def _chatroom_dir() -> Path:
    CHATROOM_DIR.mkdir(parents=True, exist_ok=True)
    return CHATROOM_DIR


def _chatroom_path(session_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(session_id or ""))
    if not safe:
        raise ValueError("无效的会话 ID")
    return _chatroom_dir() / f"{safe}.json"


def _chatroom_load(session_id: str) -> Optional[Dict[str, Any]]:
    path = _chatroom_path(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _chatroom_save(obj: Dict[str, Any]):
    obj["updated_at"] = int(time.time())
    path = _chatroom_path(obj["id"])
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")



def _chatroom_public(obj: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """剥离仅供后端 Agent 使用的工具链历史。"""
    if not isinstance(obj, dict):
        return obj
    public = dict(obj)
    public.pop("_agent_history", None)
    public.pop("_agent_total_tokens", None)
    public.pop("_agent_total_calls", None)
    return public


def _chatroom_delete(session_id: str) -> bool:
    # 删除必须和发送共用同一把会话锁。否则 Agent 正在执行时删除文件，
    # _chatroom_append_assistant() 会用旧 obj 把已删除会话重新写回来。
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(session_id or ""))
    if not safe:
        return False
    with _chatroom_session_lock(safe):
        path = _chatroom_path(safe)
        if not path.exists():
            return False
        path.unlink()
    return True


def _chatroom_pair_count(messages: list[Dict[str, Any]]) -> int:
    """会话列表中的“条数”按一问一答计 1 次；未完成的用户消息不计入。"""
    return sum(1 for m in (messages or []) if isinstance(m, dict) and m.get("role") == "assistant")


def _chatroom_text_with_attachments(text: str, attachments: Optional[list[Dict[str, Any]]] = None) -> str:
    text = str(text or "").strip()
    parts = [text] if text else []
    for att in (attachments or [])[:8]:
        name = str(att.get("name", "附件") or "附件")[:120]
        typ = str(att.get("type", "") or "")[:80]
        if typ.startswith("text/"):
            data = str(att.get("text", "") or "")[:20000]
            if data:
                parts.append(f"\n[文件：{name}]\n{data}")
            else:
                parts.append(f"\n[文件：{name}]")
        else:
            parts.append(f"\n[附件：{name}{('，'+typ) if typ else ''}]")
    return "\n".join(p for p in parts if p).strip()


def _chatroom_message_for_llm(role: str, content: str, attachments: Optional[list[Dict[str, Any]]] = None, multimodal: bool = False) -> Dict[str, Any]:
    if role != "user" or not attachments or not multimodal:
        return {"role": role, "content": content or ""}
    blocks: list[Dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for att in attachments[:8]:
        typ = str(att.get("type", "") or "")
        data = str(att.get("data", "") or "")
        if typ.startswith("image/") and data.startswith("data:image/"):
            blocks.append({"type": "image_url", "image_url": {"url": data}})
    return {"role": "user", "content": blocks or content or ""}


def _chatroom_build_llm_messages(obj: Dict[str, Any], model: str) -> list[Dict[str, Any]]:
    ep = _chatroom_endpoint_for_model(model)
    multimodal = bool(ep and ep.get("supports_multimodal"))
    llm_messages: list[Dict[str, Any]] = [{"role": "system", "content": _chatroom_system_prompt()}]
    for m in obj.get("messages", []):
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        llm_messages.append(_chatroom_message_for_llm(role, m.get("content", ""), m.get("attachments") or [], multimodal))
    return llm_messages


def _chatroom_list_sessions() -> list[Dict[str, Any]]:
    """列出聊天室会话；空目录时原子创建一个默认会话，避免前端并发新建出两个。"""
    with _chatroom_lock:
        result = []
        for path in _chatroom_dir().glob("*.json"):
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            result.append({
                "id": obj.get("id", path.stem),
                "title": obj.get("title", "新会话"),
                "model": obj.get("model", ""),
                "updated_at": obj.get("updated_at", 0),
                "message_count": _chatroom_pair_count(obj.get("messages", [])),
            })
        if not result:
            # 默认模型：优先当前轮换列表第一个
            default_model = ""
            try:
                models = _chatroom_models()
                if models:
                    default_model = str(models[0].get("model", "") or "")
            except Exception:
                default_model = ""
            obj = _chatroom_new_session(default_model, "默认会话")
            result.append({
                "id": obj.get("id"),
                "title": obj.get("title", "默认会话"),
                "model": obj.get("model", ""),
                "updated_at": obj.get("updated_at", 0),
                "message_count": 0,
            })
        result.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return result


def _chatroom_new_session(model: str, title: str = "") -> Dict[str, Any]:
    now = int(time.time())
    obj = {
        "id": uuid.uuid4().hex,
        "title": (title or "新会话").strip()[:60] or "新会话",
        "model": model or "",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    # RLock 可重入：list ensure-default 已持锁时也能安全写入
    with _chatroom_lock:
        _chatroom_save(obj)
    return obj


def _chatroom_handle_command(text: str) -> Optional[str]:
    """命令拦截：以 reminder 开头的内容一律视为 QQ 专属命令，返回提示文案；否则 None 走 LLM。"""
    reminder = _chatroom_reminder()
    stripped = (text or "").strip()
    if reminder and stripped.startswith(reminder):
        order = stripped[len(reminder):].strip()
        if not order:
            return None
        return CHATROOM_COMMAND_HINT
    return None


def _webui_short_text(value: Any, limit: int = 50) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _chatroom_log_api_request(scene: str, model: str, base_url: str, current_key: str, message_count: int, preview: str):
    host = urllib.parse.urlparse(base_url).netloc or base_url
    key_mask = (current_key[:6] + "...") if current_key else "none"
    _append_log(f"[API] {scene} -> {model} @{host} key={key_mask} msg={message_count} q={_webui_short_text(preview, 50)}", "webui")


def _chatroom_log_api_success(scene: str, model: str, total_tokens: int, reply: str):
    _append_log(f"[API] {scene} <- {model} ok tokens={int(total_tokens or 0)} a={_webui_short_text(reply, 50)}", "webui")


def _chatroom_log_api_failure(scene: str, model: str, current_key: str, error: Any):
    key_mask = (current_key[:6] + "...") if current_key else "none"
    _append_log(f"[API] {scene} xx {model} key={key_mask} err={_webui_short_text(error, 90)}", "webui")


def _chatroom_response_tokens(data: Dict[str, Any]) -> int:
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict):
        try:
            return int(usage.get("total_tokens") or 0)
        except Exception:
            return 0
    return 0


def _chatroom_scene(model: str) -> str:
    return "chatroom"


def _chatroom_complete(model: str, messages: list[Dict[str, Any]]) -> str:
    """直接向 OpenAI 兼容接口发请求获取回复。仅在后端使用 key。"""
    endpoints = _chatroom_rotation_endpoints(model)
    if not endpoints:
        raise ValueError("所选模型不可用，请在「提供商」配置里检查模型轮换列表。")
    last_error = None
    for ep in endpoints:
        base_url = (ep.get("base_url") or "").rstrip("/")
        url = base_url + "/chat/completions"
        try:
            timeout = int(ep.get("timeout_seconds", _chatroom_others().get("api_request_timeout_seconds", 60)) or 60)
        except Exception:
            timeout = 60
        display_model = ep.get("display_model") or _provider_display_model(ep.get("provider_id", ""), ep.get("model", ""))
        for key in ep.get("keys") or []:
            _chatroom_log_api_request(_chatroom_scene(model), display_model, base_url, key, len(messages), messages[-1].get("content", "") if messages else "")
            payload = json.dumps({"model": ep.get("model") or model, "messages": messages, "stream": False}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                    "User-Agent": "XcBot-WebUI-Chatroom/1.0",
                },
                method="POST",
            )
            try:
                with _make_opener().open(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                last_error = RuntimeError(_chatroom_http_error_message(e))
                _chatroom_log_api_failure(_chatroom_scene(model), display_model, key, last_error)
                continue
            except Exception as e:
                last_error = RuntimeError(f"调用模型失败：{e}")
                _chatroom_log_api_failure(_chatroom_scene(model), display_model, key, last_error)
                continue
            try:
                content = data["choices"][0]["message"]["content"]
            except Exception:
                last_error = RuntimeError(f"模型返回格式异常：{str(data)[:300]}")
                _chatroom_log_api_failure(_chatroom_scene(model), display_model, key, last_error)
                continue
            content = (content or "").rstrip("\n")
            _chatroom_log_api_success(_chatroom_scene(model), display_model, _chatroom_response_tokens(data), content)
            return content
    raise last_error or RuntimeError("所有模型均失败")


def _chatroom_stream_complete(model: str, messages: list[Dict[str, Any]]):
    """OpenAI 兼容流式输出，yield 文本增量。"""
    endpoints = _chatroom_rotation_endpoints(model)
    if not endpoints:
        raise ValueError("所选模型不可用，请在「提供商」配置里检查模型轮换列表。")
    last_error = None
    for ep in endpoints:
        base_url = (ep.get("base_url") or "").rstrip("/")
        url = base_url + "/chat/completions"
        try:
            timeout = int(ep.get("timeout_seconds", _chatroom_others().get("api_request_timeout_seconds", 60)) or 60)
        except Exception:
            timeout = 60
        display_model = ep.get("display_model") or _provider_display_model(ep.get("provider_id", ""), ep.get("model", ""))
        for key in ep.get("keys") or []:
            _chatroom_log_api_request(_chatroom_scene(model), display_model, base_url, key, len(messages), messages[-1].get("content", "") if messages else "")
            payload = json.dumps({"model": ep.get("model") or model, "messages": messages, "stream": True}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                    "User-Agent": "XcBot-WebUI-Chatroom/1.0",
                    "Accept": "text/event-stream",
                },
                method="POST",
            )
            emitted = False
            reply_parts = []
            try:
                with _make_opener().open(req, timeout=timeout) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            _chatroom_log_api_success(_chatroom_scene(model), display_model, 0, "".join(reply_parts))
                            return
                        try:
                            obj = json.loads(data)
                            delta = obj.get("choices", [{}])[0].get("delta", {}).get("content")
                            if delta:
                                emitted = True
                                reply_parts.append(str(delta))
                                yield str(delta)
                        except Exception:
                            continue
                    if emitted:
                        _chatroom_log_api_success(_chatroom_scene(model), display_model, 0, "".join(reply_parts))
                        return
            except urllib.error.HTTPError as e:
                last_error = RuntimeError(_chatroom_http_error_message(e))
                _chatroom_log_api_failure(_chatroom_scene(model), display_model, key, last_error)
                continue
            except Exception as e:
                last_error = RuntimeError(f"调用模型失败：{e}")
                _chatroom_log_api_failure(_chatroom_scene(model), display_model, key, last_error)
                continue
    raise last_error or RuntimeError("所有模型均失败")


# 单个会话保留的最大消息数与总字符数。图片以 base64 存在附件里，不设上限的话
# 会话 JSON 会无限膨胀，而且每次请求都要把整段历史重新发给模型。
CHATROOM_MAX_MESSAGES = 200
CHATROOM_MAX_CHARS = 2 * 1024 * 1024
# 隐藏的 Agent 工具链不计入可见消息上限；单独限制，避免工具结果让会话文件无限增长。
CHATROOM_AGENT_MAX_MESSAGES = 96
CHATROOM_AGENT_MAX_CHARS = 512 * 1024


def _chatroom_msg_size(msg: Dict[str, Any]) -> int:
    size = len(str(msg.get("content", "") or ""))
    for att in msg.get("attachments") or []:
        size += len(str(att.get("data", "") or "")) + len(str(att.get("text", "") or ""))
    return size


def _chatroom_trim_history(obj: Dict[str, Any]) -> None:
    """按条数和总体积裁掉最旧的消息，就地修改 obj。

    历史里的图片 base64 很占地方：不裁的话磁盘、内存和每次请求体都会持续增长。
    先按条数裁，再按总字符数从头丢，直到进入预算。
    """
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return
    if len(msgs) > CHATROOM_MAX_MESSAGES:
        del msgs[: len(msgs) - CHATROOM_MAX_MESSAGES]
    total = sum(_chatroom_msg_size(m) for m in msgs if isinstance(m, dict))
    while total > CHATROOM_MAX_CHARS and len(msgs) > 2:
        dropped = msgs.pop(0)
        if isinstance(dropped, dict):
            total -= _chatroom_msg_size(dropped)


def _chatroom_trim_agent_history(obj: Dict[str, Any]) -> None:
    """限制隐藏 Agent 历史，并从完整 user turn 开始保留。"""
    history = obj.get("_agent_history")
    if not isinstance(history, list):
        return
    cleaned = [
        dict(item) for item in history
        if isinstance(item, dict) and item.get("role") in ("user", "assistant", "tool")
    ]
    while cleaned and (
        len(cleaned) > CHATROOM_AGENT_MAX_MESSAGES
        or sum(len(json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str)) for x in cleaned)
        > CHATROOM_AGENT_MAX_CHARS
    ):
        cleaned.pop(0)
    while cleaned and cleaned[0].get("role") != "user":
        cleaned.pop(0)
    obj["_agent_history"] = cleaned


# 单条消息的硬上限。历史裁剪循环有「至少保留两条」的下限，所以单条本身超预算时
# 它无论如何都裁不掉——必须在入库前独立拒绝，不能指望事后裁剪。
CHATROOM_MAX_ONE_MESSAGE = 1024 * 1024
CHATROOM_MAX_ATTACHMENTS = 8


def _chatroom_validate_incoming(text: str, attachments: Optional[list] = None) -> None:
    """入库前校验单条消息，超限直接拒绝。"""
    atts = attachments or []
    if len(atts) > CHATROOM_MAX_ATTACHMENTS:
        raise ValueError(f"附件数量超过上限（{len(atts)} 个，最多 {CHATROOM_MAX_ATTACHMENTS} 个）")
    size = len(str(text or ""))
    for att in atts:
        if not isinstance(att, dict):
            continue
        size += len(str(att.get("data", "") or "")) + len(str(att.get("text", "") or ""))
    if size > CHATROOM_MAX_ONE_MESSAGE:
        raise ValueError(
            f"这条消息连附件共 {size} 字符，超过单条上限 {CHATROOM_MAX_ONE_MESSAGE}。"
            "请压缩图片或分几次发送。"
        )


def _chatroom_prepare_user_message(session_id: str, model: str, text: str, attachments: Optional[list[Dict[str, Any]]] = None) -> tuple[Dict[str, Any], str]:
    # 先校验原始输入：_chatroom_text_with_attachments 会把文本附件拼进正文，
    # 拼完再算大小就晚了
    _chatroom_validate_incoming(text, attachments)
    text = _chatroom_text_with_attachments(text, attachments)
    if not text:
        raise ValueError("消息内容不能为空")
    with _chatroom_lock:
        obj = _chatroom_load(session_id)
        if obj is None:
            raise ValueError("会话不存在")
        if model:
            obj["model"] = model
        now = int(time.time())
        request_id = uuid.uuid4().hex
        msg = {"role": "user", "content": text, "ts": now, "request_id": request_id}
        if attachments:
            msg["attachments"] = attachments[:8]
        obj["messages"].append(msg)
        _chatroom_trim_history(obj)
        _chatroom_trim_agent_history(obj)
        if obj.get("title", "新会话") == "新会话":
            obj["title"] = text.strip()[:30] or "新会话"
        _chatroom_save(obj)
    return obj, text


def _chatroom_progress_messages(agent_state: Optional[Dict[str, Any]]) -> list[str]:
    """收集本轮 Agent 调工具前的助手说明，兼容 SSE 事件未及时到达。"""
    if not isinstance(agent_state, dict):
        return []
    messages = []
    for text in agent_state.get("progress_messages") or []:
        text = str(text or "").strip()
        if text and text not in messages:
            messages.append(text)

    # progress 回调仅用于即时显示；真正可靠的来源是本轮保留下来的工具链。
    # 只查看最后一条用户消息之后，避免重新显示旧对话中的工具前说明。
    history = agent_state.get("history")
    if isinstance(history, list):
        last_user = max((
            index for index, item in enumerate(history)
            if isinstance(item, dict) and item.get("role") == "user"
        ), default=-1)
        for item in history[last_user + 1:]:
            if not isinstance(item, dict) or item.get("role") != "assistant" or not item.get("tool_calls"):
                continue
            text = str(item.get("content") or "").strip()
            if text and text not in messages:
                messages.append(text)
    return messages


def _chatroom_append_assistant(obj: Dict[str, Any], reply: str,
                               agent_state: Optional[Dict[str, Any]] = None,
                               progress_messages: Optional[list[str]] = None) -> Dict[str, Any]:
    with _chatroom_lock:
        fresh = _chatroom_load(obj.get("id", "")) or obj
        request_id = next((
            m.get("request_id") for m in reversed(fresh.get("messages", []))
            if m.get("role") == "user" and m.get("request_id")
        ), "")
        for content in progress_messages or []:
            content = str(content or "").strip()
            if content:
                fresh.setdefault("messages", []).append({
                    "role": "assistant", "content": content, "ts": int(time.time()),
                    "request_id": request_id,
                })
        fresh.setdefault("messages", []).append({
            "role": "assistant", "content": reply, "ts": int(time.time()),
            "request_id": request_id,
        })
        if isinstance(agent_state, dict):
            history = agent_state.get("history")
            if isinstance(history, list):
                fresh["_agent_history"] = history
            fresh["_agent_total_tokens"] = int(agent_state.get("total_tokens") or 0)
            fresh["_agent_total_calls"] = int(agent_state.get("total_calls") or 0)
            _chatroom_trim_agent_history(fresh)
        _chatroom_trim_history(fresh)
        _chatroom_save(fresh)
        return fresh

def _chatroom_remove_pending_user(session_id: str, request_id: str) -> None:
    """请求未完成时移除对应孤立 user turn。"""
    if not request_id:
        return
    with _chatroom_lock:
        obj = _chatroom_load(session_id)
        if obj is None:
            return
        messages = obj.get("messages", [])
        has_assistant = any(m.get("role") == "assistant" and m.get("request_id") == request_id for m in messages)
        if has_assistant:
            return
        obj["messages"] = [m for m in messages if not (m.get("role") == "user" and m.get("request_id") == request_id)]
        _chatroom_save(obj)


def _chatroom_send(session_id: str, model: str, text: str,
                   attachments: Optional[list[Dict[str, Any]]] = None) -> Dict[str, Any]:
    with _chatroom_session_lock(session_id):
        obj, text = _chatroom_prepare_user_message(session_id, model, text, attachments)
        request_id = next((
            m.get("request_id") for m in reversed(obj.get("messages", []))
            if isinstance(m, dict) and m.get("role") == "user"
        ), "")
        hint = _chatroom_handle_command(text)
        agent_state = None
        try:
            if hint is not None:
                reply = hint
            elif callable(_chatroom_agent_callback):
                result = _chatroom_agent_callback({
                    "id": session_id,
                    "model": obj.get("model") or model,
                    "text": text,
                    "attachments": attachments or [],
                    "agent_history": obj.get("_agent_history") or [],
                    "total_tokens": obj.get("_agent_total_tokens") or 0,
                    "total_calls": obj.get("_agent_total_calls") or 0,
                    "admin": bool(str(get_webui_config().get("access_token", "") or "").strip()),
                })
                if not isinstance(result, dict):
                    raise RuntimeError("聊天室 Agent 回调返回格式无效")
                reply = str(result.get("reply") or "")
                agent_state = result
            else:
                llm_messages = _chatroom_build_llm_messages(obj, obj.get("model") or model)
                reply = _chatroom_complete(obj.get("model") or model, llm_messages)
        except Exception:
            # 同步接口也必须和 SSE 接口一致：模型/Agent 失败时删除尚未配对的 user turn，
            # 否则下一次请求会把一条永远没有 assistant 的消息带进上下文。
            _chatroom_remove_pending_user(session_id, request_id)
            raise
        obj = _chatroom_append_assistant(
            obj, reply, agent_state=agent_state,
            progress_messages=_chatroom_progress_messages(agent_state),
        )
        return {"reply": reply, "session": _chatroom_public(obj)}

def normalize_string_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = values.splitlines()
    if not isinstance(values, list):
        return []
    return [str(x).strip() for x in values if str(x).strip()]


def merge_string_lists(*items: Any) -> list[str]:
    """合并多个字符串列表并去重，保持原有顺序。"""
    result: list[str] = []
    seen = set()
    for values in items:
        for item in normalize_string_list(values):
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def get_webui_config() -> Dict[str, Any]:
    cfg = read_json(CONFIG_PATH, {})
    webui = cfg.get("WebUI") or cfg.get("webui") or {}
    return {
        "enabled": normalize_bool_config(webui.get("enabled", True), default=True),
        "host": str(webui.get("host", "127.0.0.1")),
        "port": int(webui.get("port", 8765)),
        "access_token": str(webui.get("access_token", "")),
        "theme_preset": str(webui.get("theme_preset", "xcbot") or "xcbot"),
        "background_image": str(webui.get("background_image", "") or ""),
        "background_blur": int(float(webui.get("background_blur", 10) or 0)),
        "liquid_glass": normalize_bool_config(webui.get("liquid_glass", False), default=False),
    }


def _apply_webui_runtime_update(old_cfg: Dict[str, Any], new_cfg: Dict[str, Any]):
    """在保存 WebUI 自身配置后，尽量原地热更新 WebUI 服务。"""
    old_cfg = dict(old_cfg or {})
    new_cfg = dict(new_cfg or {})

    if old_cfg == new_cfg:
        return

    def _worker():
        global _server
        try:
            # 避免在当前 HTTP 请求尚未返回时立即关闭正在处理请求的 server。
            time.sleep(0.25)
            with _webui_reconfigure_lock:
                if not new_cfg.get("enabled", True):
                    print("🌐 WebUI 配置已变更：已禁用，正在关闭 WebUI 服务。")
                    stop_webui()
                    return

                current_server = _server
                current_changed = (
                    current_server is None
                    or str(old_cfg.get("host", "127.0.0.1")) != str(new_cfg.get("host", "127.0.0.1"))
                    or int(old_cfg.get("port", 8765)) != int(new_cfg.get("port", 8765))
                )

                if current_changed:
                    print(
                        "🌐 WebUI 配置已变更，正在热更新监听："
                        f"{old_cfg.get('host', '127.0.0.1')}:{old_cfg.get('port', 8765)} -> "
                        f"{new_cfg.get('host', '127.0.0.1')}:{new_cfg.get('port', 8765)}"
                    )
                    stop_webui()
                    start_webui(
                        host=str(new_cfg.get("host", "127.0.0.1")),
                        port=int(new_cfg.get("port", 8765)),
                        on_config_saved=_config_saved_callback,
                    )
                else:
                    print("🌐 WebUI 配置已热更新：访问参数已立即生效。")
        except Exception as e:
            print(f"WebUI 自身热更新失败: {e}")

    threading.Thread(target=_worker, name="XcBot-WebUI-HotUpdate", daemon=True).start()


def set_connection_status(state: str, text: str = "", detail: str = "") -> None:
    """供 main.py 更新 OneBot / Hyper 连接状态，WebUI 通过 /api/ui-state 异步展示。"""
    global _connection_status
    state = str(state or "unknown").strip() or "unknown"
    default_text = {
        "starting": "正在启动",
        "connecting": "连接中",
        "connected": "已连接",
        "disconnected": "已断开",
        "failed": "连接失败",
        "stopped": "已停止",
        "unknown": "未知状态",
    }.get(state, state)
    _connection_status = {
        "state": state,
        "text": str(text or default_text),
        "detail": str(detail or ""),
        "updated_at": int(time.time()),
    }


def config_fingerprint(cfg: Dict[str, Any]) -> str:
    try:
        raw = json.dumps(cfg, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        raw = str(cfg)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def collect_agent_tools(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """把 config.json 的 Agent.tools 与 AGENT_TOOL_META 默认值合并。

    只认 meta 里声明过的工具名：config 里的陌生键（旧版残留或手改错）直接忽略，
    避免 WebUI 渲染出一个后端根本没注册的工具开关。
    """
    raw = deep_get(cfg, "Agent.tools", {})
    if not isinstance(raw, dict):
        raw = {}
    merged = {}
    for meta in AGENT_TOOL_META:
        name = meta["key"]
        item = raw.get(name, {})
        if not isinstance(item, dict):
            item = {}
        level = str(item.get("level", "") or "").strip()
        merged[name] = {
            "enabled": normalize_bool_config(
                item.get("enabled", meta["default_enabled"]), default=meta["default_enabled"]
            ),
            "level": level if level in ("user", "admin") else meta["level"],
        }
    return merged


MCP_CONFIG_PATH = BASE_DIR / "data" / "mcp_server.json"

# main.py 启动后注入，用于在 WebUI 里触发 MCP 重连（重连要在机器人的事件循环里跑）
_mcp_reload_hook = None


def set_mcp_reload_hook(hook) -> None:
    global _mcp_reload_hook
    _mcp_reload_hook = hook


def read_mcp_servers() -> Dict[str, Any]:
    data = read_json(MCP_CONFIG_PATH, {})
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}


def save_mcp_servers(servers: Dict[str, Any]) -> None:
    cleaned = {}
    for name, cfg in servers.items():
        name = str(name or "").strip()
        if not name or name.startswith("_") or not isinstance(cfg, dict):
            continue
        url = str(cfg.get("url", "") or "").strip()
        command = str(cfg.get("command", "") or "").strip()
        if not url and not command:
            # 两者都空是 WebUI 的占位行，保存时丢弃
            continue
        item = {"enabled": normalize_bool_config(cfg.get("enabled", True), default=True)}
        if url:
            item["url"] = url
            if isinstance(cfg.get("headers"), dict):
                item["headers"] = {str(k): str(v) for k, v in cfg["headers"].items()}
        else:
            item["command"] = command
            args = cfg.get("args", [])
            item["args"] = [str(x) for x in args if str(x).strip()] if isinstance(args, list) else []
            if isinstance(cfg.get("env"), dict):
                item["env"] = {str(k): str(v) for k, v in cfg["env"].items()}
        cleaned[name] = item
    write_json(MCP_CONFIG_PATH, {"mcpServers": cleaned})


def get_mcp_state() -> Dict[str, Any]:
    try:
        import bot.agent_mcp as agent_mcp
        available, reason = agent_mcp.is_available()
        registered = agent_mcp.registered_tool_names()
    except Exception as e:
        available, reason, registered = False, f"MCP 模块加载失败：{e}", []
    return {
        "available": available,
        "error": reason,
        "servers": read_mcp_servers(),
        "registered_tools": registered,
        "config_path": str(MCP_CONFIG_PATH),
    }


def reload_mcp_servers() -> str:
    if not callable(_mcp_reload_hook):
        return "机器人主程序尚未就绪，无法重连 MCP。请稍后再试或重启机器人。"
    try:
        return str(_mcp_reload_hook() or "已触发 MCP 重连，请稍后刷新查看结果。")
    except Exception as e:
        return f"触发 MCP 重连失败：{e}"


def collect_config_bundle() -> Dict[str, Any]:
    cfg = read_json(CONFIG_PATH, {})
    features = dict(DEFAULT_FEATURE_SWITCHES)
    raw_features = cfg.get("FeatureSwitches", {})
    if isinstance(raw_features, dict):
        for key in list(features.keys()):
            if key in raw_features:
                features[key] = normalize_bool_config(raw_features.get(key), default=features[key])
    owner_users = normalize_string_list(cfg.get("owner", []))
    root_users = normalize_string_list(deep_get(cfg, "Others.ROOT_User", []))
    # WebUI 中“管理用户”是唯一入口；如果历史配置里 owner / ROOT_User 不一致，优先保留 owner，
    # 同时合并 ROOT_User，避免旧字段把刚保存的页面值覆盖成空或旧值。
    manage_users = merge_string_lists(owner_users, root_users)
    super_users = manage_users[:]
    blacklist_file = normalize_string_list(cfg.get("black_list", []))
    return {
        "config_json": cfg,
        "feature_switches": features,
        "feature_meta": FEATURE_META,
        "agent_tools": collect_agent_tools(cfg),
        # 只下发要展示的工具：隐藏工具（只读查询、只作用于当前会话的操作）
        # 既不出现在页面上，也不写进 config.json
        "agent_tool_meta": visible_tools(),
        "agent_tool_groups": AGENT_TOOL_GROUPS,
        "ui_schema": build_ui_schema(cfg),
        "super_users": super_users,
        "manage_users": manage_users,
        "blacklist_file": blacklist_file,
        "paths": {
            "config_json": str(CONFIG_PATH),
            "runtime_log": str(LOG_FILE),
        },
        "config_fingerprint": config_fingerprint(cfg),
    }


def save_config_bundle(data: Dict[str, Any]):
    """保存整份配置。整段读-改-写必须在同一把锁内：ThreadingHTTPServer 会并发
    处理保存请求，两个请求各读一份旧配置、各改不同字段再写回，
    后写的会把前一个人的改动整段抹掉（原子替换只防半截文件，防不了这个）。"""
    with config_transaction(CONFIG_PATH):
        return _save_config_bundle_locked(data)


def _save_config_bundle_locked(data: Dict[str, Any]):
    old_webui_cfg = get_webui_config()
    cfg = data.get("config_json", read_json(CONFIG_PATH, {}))
    if not isinstance(cfg, dict):
        cfg = read_json(CONFIG_PATH, {})
        if not isinstance(cfg, dict):
            cfg = {}

    feature_switches = dict(DEFAULT_FEATURE_SWITCHES)
    raw = cfg.get("FeatureSwitches", {})
    if isinstance(raw, dict):
        for key in list(feature_switches.keys()):
            if key in raw:
                feature_switches[key] = normalize_bool_config(raw.get(key), default=feature_switches[key])
    if "feature_switches" in data and isinstance(data["feature_switches"], dict):
        for key in list(feature_switches.keys()):
            if key in data["feature_switches"]:
                feature_switches[key] = normalize_bool_config(data["feature_switches"][key], default=feature_switches[key])
    cfg["FeatureSwitches"] = {
        **({"_comment": raw.get("_comment", "功能热开关")} if isinstance(raw, dict) else {"_comment": "功能热开关"}),
        **feature_switches,
    }

    if "manage_users" in data:
        manage_users = normalize_string_list(data.get("manage_users", []))
        cfg["owner"] = manage_users
        others = cfg.setdefault("Others", {})
        if not isinstance(others, dict):
            others = {}
            cfg["Others"] = others
        others["ROOT_User"] = manage_users

    if "blacklist_file" in data:
        cfg["black_list"] = normalize_string_list(data.get("blacklist_file", []))

    if "agent_tools" in data and isinstance(data["agent_tools"], dict):
        agent_section = cfg.setdefault("Agent", {})
        if not isinstance(agent_section, dict):
            agent_section = {}
            cfg["Agent"] = agent_section
        raw_tools = agent_section.get("tools", {})
        if not isinstance(raw_tools, dict):
            raw_tools = {}
        merged_tools = {}
        # 保留 _comment，其余按 meta 白名单重建，顺序与 WebUI 展示一致
        if raw_tools.get("_comment"):
            merged_tools["_comment"] = raw_tools["_comment"]
        allowed = configurable_keys()
        # 一张卡片背后可能联动多个工具（如「文件与代码」= 5 个文件工具 + 2 个执行工具）。
        # 前端只发代表工具的状态，这里展开到同卡片的全部成员，否则其余成员会被
        # 当成"没提交"而退回默认值，用户关了开关却发现工具还能用。
        incoming_all = {}
        for name, value in (data["agent_tools"] or {}).items():
            if name not in allowed or not isinstance(value, dict):
                continue
            for member in card_members(name):
                incoming_all[member] = value
        for meta in AGENT_TOOL_META:
            name = meta["key"]
            # 隐藏工具不落配置：它们恒为默认值，写进去只会让 config 变长
            if meta.get("hidden"):
                continue
            incoming = incoming_all.get(name, {})
            existing = raw_tools.get(name, {})
            if not isinstance(existing, dict):
                existing = {}
            level = str(incoming.get("level", existing.get("level", meta["level"])) or "").strip()
            merged_tools[name] = {
                "enabled": normalize_bool_config(
                    incoming.get("enabled", existing.get("enabled", meta["default_enabled"])),
                    default=meta["default_enabled"],
                ),
                "level": level if level in ("user", "admin") else meta["level"],
            }
        agent_section["tools"] = merged_tools

    others = cfg.setdefault("Others", {})
    if not isinstance(others, dict):
        others = {}
        cfg["Others"] = others

    raw_providers = others.get("llm_providers", [])
    if isinstance(raw_providers, list):
        seen_provider_ids = set()
        cleaned_providers = []
        for index, provider in enumerate(raw_providers, start=1):
            if not isinstance(provider, dict):
                continue
            provider_id = str(provider.get("id", "") or "").strip()
            # 完全空白的渠道是初始化占位，保存时自动移除。
            raw_models = provider.get("models", []) if isinstance(provider.get("models", []), list) else []
            raw_embedding_models = provider.get("embedding_models", []) if isinstance(provider.get("embedding_models", []), list) else []
            has_model_name = any(
                isinstance(item, str) and item.strip()
                or isinstance(item, dict) and str(item.get("name", "") or item.get("model", "") or "").strip()
                for item in raw_embedding_models
            ) or any(
                isinstance(item, str) and item.strip()
                or isinstance(item, dict) and str(item.get("name", "") or item.get("model", "") or "").strip()
                for item in raw_models
            )
            has_provider_content = bool(
                provider_id
                or str(provider.get("base_url", "") or "").strip()
                or _normalize_provider_keys(provider.get("keys", []))
                or has_model_name
            )
            if not has_provider_content:
                continue
            if not provider_id:
                raise ValueError(f"第 {index} 个渠道填写了配置，但 ID 不能为空")
            if provider_id in seen_provider_ids:
                raise ValueError(f"渠道 ID 重复：{provider_id}。每个渠道必须使用唯一名称")
            seen_provider_ids.add(provider_id)

            seen_model_names = set()
            raw_models = provider.get("models", [])
            cleaned_models = []
            if isinstance(raw_models, list):
                for model_cfg in raw_models:
                    if isinstance(model_cfg, str):
                        model_cfg = {"name": model_cfg, "enabled": True}
                    if not isinstance(model_cfg, dict):
                        continue
                    model_name = str(model_cfg.get("name", "") or model_cfg.get("model", "") or "").strip()
                    # 空白模型是 WebUI 的占位行，保存时直接忽略；只有具名模型参与校验。
                    if not model_name:
                        continue
                    if model_name in seen_model_names:
                        raise ValueError(f"渠道 {provider_id} 中模型名称重复：{model_name}")
                    seen_model_names.add(model_name)
                    cleaned = dict(model_cfg)
                    cleaned["name"] = model_name
                    cleaned.pop("model", None)
                    cleaned_models.append(cleaned)
                provider["models"] = cleaned_models
            raw_embedding_models = provider.get("embedding_models", [])
            cleaned_embedding_models = []
            if isinstance(raw_embedding_models, list):
                seen_embedding_names = set()
                for item in raw_embedding_models:
                    if isinstance(item, str):
                        item = {"name": item, "enabled": True}
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name", "") or item.get("model", "") or "").strip()
                    if not name or name in seen_embedding_names:
                        continue
                    seen_embedding_names.add(name)
                    cleaned = dict(item)
                    cleaned["name"] = name
                    cleaned.pop("model", None)
                    cleaned["enabled"] = normalize_bool_config(item.get("enabled", True), default=True)
                    try:
                        cleaned["dimensions"] = max(0, int(item.get("dimensions", 0) or 0))
                    except (TypeError, ValueError):
                        cleaned["dimensions"] = 0
                    cleaned_embedding_models.append(cleaned)
            provider["embedding_models"] = cleaned_embedding_models
            cleaned_providers.append(provider)
        others["llm_providers"] = cleaned_providers

    knowledge = cfg.setdefault("KnowledgeBase", {})
    if not isinstance(knowledge, dict):
        knowledge = {}
        cfg["KnowledgeBase"] = knowledge
    try:
        chunk_size = max(200, min(int(knowledge.get("chunk_size", 1000) or 1000), 5000))
    except (TypeError, ValueError):
        chunk_size = 1000
    try:
        chunk_overlap = max(0, min(int(knowledge.get("chunk_overlap", 150) or 0), 1000))
    except (TypeError, ValueError):
        chunk_overlap = 150
    knowledge["chunk_size"] = chunk_size
    knowledge["chunk_overlap"] = min(chunk_overlap, chunk_size // 2)

    sync_provider_config(others)
    sync_personality_presets(others)
    cfg.pop("split_reply_quote", None)

    data["config_json"] = cfg
    write_json(CONFIG_PATH, cfg)
    cleanup_legacy_config_files()
    force_apply_llm_endpoints_from_config(cfg)
    _apply_webui_runtime_update(old_webui_cfg, get_webui_config())
    if callable(_config_saved_callback):
        _config_saved_callback()


def deep_get(data: Dict[str, Any], path: str, default=None):
    cur = data
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def deep_set(data: Dict[str, Any], path: str, value):
    cur = data
    parts = path.split(".")
    for key in parts[:-1]:
        if not isinstance(cur.get(key), dict):
            cur[key] = {}
        cur = cur[key]
    cur[parts[-1]] = value


def field(path: str, label: str, typ="text", desc="", default=None, options=None, category="基础", min=None, max=None) -> Dict[str, Any]:
    return {"path": path, "label": label, "type": typ, "desc": desc, "default": default, "options": options or [], "category": category, "min": min, "max": max}


def build_ui_schema(cfg: Dict[str, Any]) -> list[Dict[str, Any]]:
    return [
        {"key": "welcome", "title": "欢迎", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>', "desc": "", "fields": []},
        {"key": "stats", "title": "数据统计", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" x2="18" y1="20" y2="10"/><line x1="12" x2="12" y1="20" y2="4"/><line x1="6" x2="6" y1="20" y2="14"/></svg>', "desc": "消息数、模型调用历史、模型排名与最近 1 天 Tokens Top 10", "fields": []},
        {"key": "bot", "title": "机器人", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="10" x="3" y="11" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" x2="8" y1="16" y2="16"/><line x1="16" x2="16" y1="16" y2="16"/></svg>', "desc": "机器人名称、触发词和命令前缀", "fields": [
            field("Others.bot_name", "中文名", "text"),
            field("Others.bot_name_en", "英文名", "text"),
            field("Others.reminder", "命令前缀", "text", "例如 /帮助 中的 /"),
            field("Others.robot_name_triggers", "触发词", "list", "一行一个，群里提到会触发回复"),
        ]},
        {"key": "ai", "title": "AI 配置", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/><path d="M5 3v4"/><path d="M19 17v4"/><path d="M3 5h4"/><path d="M17 19h4"/></svg>', "desc": "对话行为与分段设置", "fields": [
            field("Others.context_max_messages", "上下文最大消息数", "number"),
            field("Others.api_failure_cooldown_seconds", "失败冷却秒数", "number", "单个 API / Key 调用失败后，冷却多久再重试", 5),
            field("Others.llm_reply_failover_keywords", "回复切换关键词", "list", "一行一个。若模型回复命中其中任一关键词，则丢弃该回复并按现有失败冷却逻辑自动切换到下一个 API"),
            field("Others.llm_split.enabled", "启用 LLM 分段回复", "bool", "仅对大模型生成结果生效，不影响普通群聊回复是否引用"),
            field("Others.llm_split.mode", "LLM 分段模式", "select", "auto_prompt=大模型自主分段；regex=按正则切分模型输出", "auto_prompt", ["auto_prompt", "regex"]),
            field("Others.llm_split.prompt_suffix", "自主分段提示词", "textarea", "模式一使用。会自动追加到每次 LLM 用户消息后。建议保留 <split> 分隔符说明"),
            field("Others.llm_split.split_regex", "分段正则表达式", "textarea", "模式二使用。用于识别分段点。建议：.*?[。？！~]+|.+$"),
            field("Others.llm_split.filter_regex", "内容过滤正则表达式", "textarea", "仅模式二使用。对每段文本做清理，例如移除换行：\\n|\\r。模式一不受它影响，模型自己排的换行会原样保留"),
            field("Others.llm_split.max_chars_no_split", "超过多少字不分段", "number", "最终要发送的整条内容超过[ ]字时，忽略 <split>/正则分段，改为单条发送；填 0 表示不限制", 0),
        ]},
        {"key": "providers", "title": "提供商", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg>', "desc": "配置提供商、对话模型、嵌入模型并设置轮换顺序", "fields": [
            field("Others.llm_providers", "提供商（对话 / 嵌入）", "providers", "对话模型与嵌入模型共用同一提供商的 Base URL 和 Key；嵌入模型在提供商卡片的“嵌入”区域配置。"),
            field("Others.llm_rotation", "模型轮换", "rotation"),
            field("Others.api_multimodal_model", "多模态图片模型", "multimodal_model", "主模型不支持多模态且用户发送图片时使用的多模态模型"),
            field("Others.api_multimodal_image_mode", "图片处理模式", "select", "relay=多模态模型先转述图片，再交给主模型回复；direct=直接由多模态模型回复图片消息", "relay", ["relay", "direct"]),
        ]},
        {"key": "knowledge", "title": "知识库", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/></svg>', "desc": "上传文档，配置向量检索或 SQLite 全文检索", "fields": [
            field("KnowledgeBase.enabled", "启用知识库", "bool", "开启后 Agent 会自动检索 WebUI 上传的知识库文档。"),
            field("KnowledgeBase.vector_mode_enabled", "使用向量模型", "bool", "默认开启；关闭后回退到 SQLite FTS 全文检索，不使用向量模型。"),
            field("KnowledgeBase.embedding_model_ref", "向量模型", "embedding_model_ref", "按“提供商/模型名”选择已启用的嵌入模型。"),
            field("KnowledgeBase.top_k", "知识库召回数量", "number", "每次最多注入多少个相关片段。", 5, min=1, max=20),
            field("KnowledgeBase.chunk_size", "知识库分块大小", "number", "文档切分的近似字符数。", 1000, min=200, max=5000),
            field("KnowledgeBase.chunk_overlap", "知识库分块重叠", "number", "相邻片段重叠的近似字符数。", 150, min=0, max=1000),
            field("KnowledgeBase.max_context_chars", "知识库最大注入字数", "number", "限制每次注入模型上下文的知识库字数。", 8000, min=500, max=30000),
        ]},
        {"key": "persona", "title": "人格设定", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>', "desc": "编辑人设", "fields": [
            field("Others.personality_presets", "人格预设", "persona_presets"),
            field("Others.active_personality_preset", "当前预设", "text"),
            field("Others.personality_prompt", "编辑人设", "textarea", "可使用 {bot_name} 与 {user_name} 占位符"),
        ]},
        {"key": "features", "title": "功能配置", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="7" height="7" x="14" y="3" rx="1"/><path d="M10 21V8a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5a1 1 0 0 0-1-1H3"/></svg>', "desc": "配置功能", "fields": [
            field("Others.sensitive_words", "屏蔽词", "list", "一行一条，格式「原词=替换成的词」；只写原词则删掉它。⚠️ 会替换所有出现位置，正常聊天里的同名词也会被改"),
            field("Others.emoji_plus_one_cooldown_seconds", "表情 +1 冷却秒数", "number", "单个表情自动复读的防抖时间"),
            field("Others.weak_blacklist_trigger_probability", "弱黑名单回复概率", "number", "0 到 1 之间，越小越容易拦截"),
            field("Others.weak_blacklist_users", "弱黑名单用户", "list", "一行一个 QQ 号"),
            field("Others.group_random_reply_probability", "群聊概率触发概率", "number", "普通群消息命中该概率时，机器人会主动接话。支持 0~1，也兼容 0~100；填 0 表示关闭"),
            field("Others.group_random_reply_quote", "群聊概率触发时引用消息", "bool", "开启后，概率触发的回复会引用原消息；关闭则直接发送"),
            field("Others.group_chat_context_max_messages", "群聊上下文最大条数", "number", "功能配置里开启「群聊上下文感知」后，每个群旁听缓冲最多保留多少条", 30, min=1, max=300),
            field("Others.poke_cooldown_seconds", "拍一拍冷却秒数", "number", "拍一拍自动回复的防抖时间"),
            field("Others.group_join_welcome_text", "入群欢迎语", "textarea", "完整欢迎语，支持换行。占位符：{at} @新成员、{user_nickname} 昵称、{user_id} QQ号、{group_id} 群号、{bot_name} 机器人名字。留空则使用默认文案"),
            field("Others.group_join_welcome_send_avatar", "欢迎时发送头像图片", "bool", "开启后在欢迎消息中附上新成员头像；关闭后只发 @ + 欢迎语"),
            field("Others.summary_per_day_limit", "每日总结次数", "number", "每个群每天允许总结的次数"),
            field("Others.summary_max_messages", "每次最多总结消息数", "number", "单次群聊总结最多读取多少条消息"),
            field("Others.compression_threshold", "压缩触发阈值", "number", "消息达到多少条后允许触发压缩"),
            field("Others.compression_keep_recent", "压缩保留最近消息", "number", "压缩时保留最近多少轮（完整对话轮次）"),
            field("Others.auto_compress_after_messages", "自动压缩消息数", "number", "消息累计到多少条时自动尝试压缩"),
            field("Others.system.log_retention_days", "日志保留天数", "number", "data/webui 下超出天数的 runtime-*.log 会被自动删除。每小时检查一次，保存配置时立即执行一次", 7, category="系统设置", min=1, max=365),
            field("Others.system.trace_max_records", "追踪保存条数", "number", "AI 对话追踪最多保留多少条记录，超出丢最旧的。条数越多占用磁盘和内存越大", 100, category="系统设置", min=1, max=1000),
        ]},
        {"key": "agent", "title": "Agent", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg>', "desc": "让 AI 自主调用工具：联网搜索、调用插件、操作 QQ、定时提醒、读写文件、执行代码、接入 MCP", "fields": [
            field("Agent.enabled", "启用 Agent", "bool", "总开关。关闭时完全不向模型传 tools，与旧版行为一致"),
            field("Agent.max_rounds", "最大工具轮数", "number", "越大越能完成多步骤任务（搜索→读网页→再搜→总结），代价是 token 消耗上升", 30, min=1, max=50),
            field("Agent.retry_attempts", "失败后额外重试次数", "number", "当前模型/Key 失败后再试几次；用尽后按轮换顺序切换下一个模型/Key。默认 1，设置为 0 表示直接轮换", 1, min=0, max=5),
            field("Agent.clear_workspace_on_reset", "/reset 命令清空工作区", "bool", "开启后 /reset 会同时清空当前会话的 Agent 工作区；关闭后仍清除聊天记忆，但保留工作区文件"),
            field("Agent.tool_result_max_chars", "工具结果最大字数", "number", "超出部分写入 data/agent_overflow，只回灌预览+路径，AI 可用 file_read 读全文", 8000, min=500, max=60000),
            field("Agent.tool_timeout", "工具超时秒数", "number", "单个工具的执行上限", 120, min=10, max=600),
            field("Agent.parallel_tools", "并发执行工具", "bool", "同一轮多个工具并发跑，更快且不增加模型请求次数"),
field("Agent.show_time", "显示当前时间", "bool", "开启后每次对话在用户消息末尾附加当前时间，让 AI 知道现在几点"),
            field("Agent.search.provider", "搜索源", "select", "auto=有 Key 优先付费源，否则回落百度/DuckDuckGo", "auto", ["auto", "tavily", "bocha", "baidu", "duckduckgo"]),
            field("Agent.search.tavily_api_key", "Tavily API Key", "password", "https://tavily.com 有免费额度，留空则不使用"),
            field("Agent.search.bocha_api_key", "博查 API Key", "password", "国内搜索源，留空则不使用"),
            field("Agent.exec_timeout", "代码执行超时秒数", "number", "execute_shell / execute_python 的执行上限", 60, min=5, max=600),
        ]},
        {"key": "store", "title": "插件商店", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" x2="21" y1="6" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/></svg>', "desc": "浏览并安装来自插件商店的插件", "fields": []},
        {"key": "security", "title": "权限/名单", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2-1 4-2 7-2 2.5 0 4.5 1 6.5 2a1 1 0 0 1 1 1v7z"/><path d="m9 12 2 2 4-4"/></svg>', "desc": "设置管理用户和黑名单", "fields": [
            field("manage_users", "管理用户", "list", "唯一高权限入口，一行一个 "),
            field("black_list", "配置黑名单", "list", "用户号或群号，一行一个"),
        ]},
        {"key": "connection", "title": "连接", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>', "desc": "OneBot / Hyper 连接参数", "fields": [
            field("Connection.mode", "连接模式", "select", options=["FWS"]),
            field("Connection.host", "连接地址", "text"),
            field("Connection.port", "连接端口", "number"),
            field("Connection.listener_host", "监听地址", "text"),
            field("Connection.listener_port", "监听端口", "number"),
            field("Connection.retries", "重试次数", "number"),
            field("Connection.access_token", "OneBot 连接 Token", "password", "NapCat / OneBot WebSocket 认证 Token；仅 protocol=OneBot 时使用，保存后会重启连接。"),
            field("protocol", "协议", "select", options=["OneBot", "Satori"]),
            field("Log_level", "日志等级", "select", options=["DEBUG", "INFO", "WARNING", "ERROR"]),
        ]},
        {"key": "webui", "title": "WebUI", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/></svg>', "desc": "Web 管理界面自身参数和外观", "fields": [
            field("WebUI.host", "监听地址", "text"),
            field("WebUI.port", "监听端口", "number"),
            field("WebUI.access_token", "访问 Token", "password", "暴露到公网时请务必设置"),
            field("Others.github_repo", "GitHub 更新仓库", "text", "格式 owner/repo，例如 Qzy327422/XcBot；留空使用默认仓库"),
            field("Others.github_download_mirrors", "GitHub 备用更新镜像", "list", "一行一个镜像前缀。检查/下载更新时先直连 GitHub，失败后按顺序尝试这些地址"),
            field("Others.http_proxy", "HTTP 代理", "text", "格式 http://127.0.0.1:7890，影响模型调用与 GitHub 请求；留空不使用代理"),
            field("WebUI.theme_preset", "主题色", "select", "选择一套背景主题色", "aurora", ["aurora", "midnight", "sakura", "forest", "sunset", "ocean"]),
            field("WebUI.background_image", "自定义背景图片", "background_image", "从本机选一张图，点「保存设置」时上传"),
            field("WebUI.background_blur", "背景模糊度", "number", "0 到 40，数值越大越柔和", 10, min=0, max=40),
            field("WebUI.liquid_glass", "仿苹果液体玻璃 UI", "bool", "⚠️ 低配机可能会出现卡顿，请自行决定是否开启"),
        ]},
        {"key": "trace", "title": "追踪", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" x2="15.42" y1="13.51" y2="17.49"/><line x1="15.41" x2="8.59" y1="6.51" y2="10.49"/></svg>', "desc": "最近 24 小时每次 AI 对话的发送链路、模型调用链路、系统提示词与 token 明细", "fields": []},
        {"key": "logs", "title": "实时日志", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>', "desc": "查看完整运行日志", "fields": []},
    ]


def get_ui_value(bundle: Dict[str, Any], path: str, default=None):
    if path == "manage_users":
        return bundle.get("manage_users", [])
    if path == "black_list":
        return bundle.get("blacklist_file", deep_get(bundle.get("config_json", {}), path, default) or [])
    if path == "KnowledgeBase.embedding_model_ref":
        cfg = bundle.get("config_json", {})
        current = str(deep_get(cfg, path, "") or "").strip()
        if current:
            return current
        provider_id = str(deep_get(cfg, "KnowledgeBase.embedding_provider_id", "") or "").strip()
        model = str(deep_get(cfg, "KnowledgeBase.embedding_model", "") or "").strip()
        return f"{provider_id}/{model}" if provider_id and model else ""
    return deep_get(bundle.get("config_json", {}), path, default)


def set_ui_value(payload: Dict[str, Any], path: str, value):
    if path == "manage_users":
        payload["manage_users"] = value
        payload["super_users"] = value
        return
    if path == "black_list":
        payload["blacklist_file"] = value
        deep_set(payload.setdefault("config_json", {}), path, value)
        return
    if path == "KnowledgeBase.embedding_model_ref":
        ref = str(value or "").strip()
        cfg = payload.setdefault("config_json", {})
        deep_set(cfg, path, ref)
        matches = []
        providers = deep_get(cfg, "Others.llm_providers", [])
        for provider in providers if isinstance(providers, list) else []:
            if not isinstance(provider, dict):
                continue
            provider_id = str(provider.get("id", "") or "").strip()
            models = provider.get("embedding_models", [])
            for item in models if isinstance(models, list) else []:
                name = (
                    str(item.get("name", "") or item.get("model", "") or "").strip()
                    if isinstance(item, dict) else str(item or "").strip()
                )
                if name and f"{provider_id}/{name}" == ref:
                    matches.append((provider_id, name))
        provider_id, model = matches[0] if len(matches) == 1 else ("", "")
        deep_set(cfg, "KnowledgeBase.embedding_provider_id", provider_id)
        deep_set(cfg, "KnowledgeBase.embedding_model", model)
        return
    deep_set(payload.setdefault("config_json", {}), path, value)


def collect_ui_state(log_limit: int = 100) -> Dict[str, Any]:
    bundle = collect_config_bundle()
    values = {}
    for section in bundle["ui_schema"]:
        for item in section.get("fields", []):
            values[item["path"]] = get_ui_value(bundle, item["path"], item.get("default"))
    return {**bundle, "form_values": values, "status": get_status(), "logs": get_recent_logs(log_limit), "statistics": collect_statistics()}


def save_ui_state(data: Dict[str, Any]):
    # 这里读出来的 cfg 会一路带到 save_config_bundle 写回去，
    # 整段都要在同一事务内，否则中途被别的请求改过的字段会丢
    with config_transaction(CONFIG_PATH):
        result = _save_ui_state_locked(data)
    # 系统设置改动后立即生效：同步追踪条数上限、按新保留天数清一次日志。
    # 放在事务外，避免清理 IO 拖长持锁时间。
    try:
        apply_trace_max_records()
        prune_old_logs(force=True)
    except Exception:
        pass
    return result


def _save_ui_state_locked(data: Dict[str, Any]):
    cfg = read_json(CONFIG_PATH, {})
    payload = {
        "config_json": cfg,
    }
    values = data.get("form_values", {}) if isinstance(data, dict) else {}
    for path, value in values.items():
        set_ui_value(payload, path, value)
    if isinstance(values, dict) and "manage_users" in values:
        payload["manage_users"] = values.get("manage_users") or []
        payload["super_users"] = values.get("manage_users") or []
    if isinstance(values, dict) and "black_list" in values:
        payload["blacklist_file"] = values.get("black_list") or []
    if isinstance(data, dict):
        for key in ("feature_switches", "super_users", "manage_users", "blacklist_file", "agent_tools"):
            if key in data:
                if key in {"super_users", "manage_users"} and isinstance(values, dict) and "manage_users" in values:
                    continue
                if key == "blacklist_file" and isinstance(values, dict) and "black_list" in values:
                    continue
                payload[key] = data[key]
    payload["manage_users"] = normalize_string_list(payload.get("manage_users", []))
    payload["blacklist_file"] = normalize_string_list(payload.get("blacklist_file", []))
    # 调 _locked 版本：外层已经持有事务锁，再走 save_config_bundle 会重入死锁
    _save_config_bundle_locked(payload)


def get_recent_logs(limit: int = 100) -> list[Dict[str, str]]:
    # 顺带做一次日志清理，内部按小时防抖，不会每次都扫目录
    prune_old_logs()
    try:
        limit = max(1, min(int(limit), 5000))
    except (TypeError, ValueError):
        limit = 100
    with _log_lock:
        logs = list(_log_buffer)[-limit:]
    # 内存足够时保留 ANSI 颜色；刚重启时内存通常只有几条启动日志，不能因此
    # 完全跳过当天磁盘日志，否则“实时日志”页看起来像只保留了几条。
    if len(logs) >= limit:
        return logs
    today = _log_file()
    if today.exists():
        lines = _iter_runtime_log_lines(limit)
        if lines:
            rows = []
            pattern = re.compile(r"^\[([^]]*)\]\s+\[([^]]*)\]\s?(.*)$")
            for line in lines:
                match = pattern.match(line)
                if match:
                    rows.append({"time": match.group(1), "stream": match.group(2), "message": match.group(3)})
                else:
                    rows.append({"time": "", "stream": "file", "message": line})
            return rows[-limit:]
    return logs


def _iter_runtime_log_lines(limit: int = 20000) -> list[str]:
    """读日志尾部若干行。

    不能用 read_text().splitlines()[-limit:]：那会把整个日志文件读进内存再丢掉
    绝大部分，跑一天的日志能有几十 MB，低配机器上光这一下就够卡。
    改成从文件末尾按块回读，只解码真正需要的那一段。
    """
    today = _log_file()
    if not today.exists():
        return []
    try:
        # 平均一行按 200 字节估，多留一倍余量；上限 8MB 防止极端长行把内存吃穿
        want_bytes = min(max(limit * 400, 65536), 8 * 1024 * 1024)
        with open(today, "rb") as fp:
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            start = max(0, size - want_bytes)
            fp.seek(start)
            chunk = fp.read()
        text = chunk.decode("utf-8", errors="replace")
        if start > 0:
            # 起点可能切在半行中间，丢掉第一段残行
            nl = text.find("\n")
            text = text[nl + 1:] if nl >= 0 else ""
        return text.splitlines()[-limit:]
    except Exception:
        return []


# ==================== 系统设置：日志保留 / 追踪条数 ====================

DEFAULT_LOG_RETENTION_DAYS = 7
DEFAULT_TRACE_MAX_RECORDS = 100
# 日志清理防抖：进程内最多每小时扫一次 logs 目录
_LOG_PRUNE_INTERVAL = 3600.0
_last_log_prune = 0.0
_log_prune_lock = threading.Lock()


def get_system_settings() -> Dict[str, int]:
    """读系统设置（日志保留天数 / 追踪条数上限）。"""
    cfg = read_json(CONFIG_PATH, {})
    others = cfg.get("Others", {}) if isinstance(cfg.get("Others"), dict) else {}
    sysc = others.get("system", {}) if isinstance(others.get("system"), dict) else {}

    def _int_of(key: str, fallback: int, low: int, high: int) -> int:
        try:
            return max(low, min(int(sysc.get(key, fallback)), high))
        except (TypeError, ValueError):
            return fallback

    return {
        "log_retention_days": _int_of("log_retention_days", DEFAULT_LOG_RETENTION_DAYS, 1, 365),
        "trace_max_records": _int_of("trace_max_records", DEFAULT_TRACE_MAX_RECORDS, 1, 1000),
    }


def prune_old_logs(force: bool = False) -> int:
    """删掉超出保留天数的旧日志文件，返回删除数量。

    文件名形如 runtime-YYYY-MM-DD.log，按文件名里的日期判断，
    不用 mtime——手工拷贝或恢复备份会把 mtime 打乱。
    """
    global _last_log_prune
    now = time.time()
    with _log_prune_lock:
        if not force and (now - _last_log_prune) < _LOG_PRUNE_INTERVAL:
            return 0
        _last_log_prune = now
    try:
        days = get_system_settings()["log_retention_days"]
        cutoff = (datetime.now() - timedelta(hours=4) - timedelta(days=days)).date()
        removed = 0
        if not LOG_DIR.exists():
            return 0
        with os.scandir(LOG_DIR) as it:
            for de in it:
                name = de.name
                if not (name.startswith("runtime-") and name.endswith(".log")):
                    continue
                try:
                    d = datetime.strptime(name[8:-4], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if d < cutoff:
                    try:
                        os.remove(de.path)
                        removed += 1
                    except OSError:
                        pass
        return removed
    except Exception:
        return 0


def apply_trace_max_records() -> None:
    """把配置里的追踪条数上限同步到 TraceStore。"""
    try:
        _trace_store().set_max_records(get_system_settings()["trace_max_records"])
    except Exception:
        pass


def _parse_log_timestamp(text: str) -> Optional[int]:
    try:
        return int(datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return None


_API_REQ_RE = re.compile(
    r"^\[(?P<time>[^\]]+)\] \[[^\]]+\] \[API\] (?P<scene>.+?) -> (?P<model>.+?) @(?P<host>[^\s]+) key=(?P<key>[^\s]+) msg=(?P<msg>\d+) q=(?P<preview>.*)$"
)
_API_OK_RE = re.compile(
    r"^\[(?P<time>[^\]]+)\] \[[^\]]+\] \[API\] (?P<scene>.+?) <- (?P<model>.+?) ok tokens=(?P<tokens>\d+) a=(?P<reply>.*)$"
)
_API_FAIL_RE = re.compile(
    r"^\[(?P<time>[^\]]+)\] \[[^\]]+\] \[API\] (?P<scene>.+?) xx (?P<model>.+?) key=(?P<key>[^\s]+) err=(?P<error>.*)$"
)
# 三条正则都要求行里有 [API]，先做一次廉价的子串判断能跳过绝大多数行
_API_MARK = "[API] "


def _pretty_scene(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("group_"):
        return f"群聊 {raw[6:]}"
    if raw.startswith("private_"):
        return f"私聊 {raw[8:]}"
    return raw


def _stat_new_accumulator() -> Dict[str, Any]:
    return {
        "total_messages": 0,
        "message_trend": Counter(),
        "message_scene": Counter(),
        "api_request_history": [],
        "model_rank": defaultdict(lambda: {"calls": 0, "tokens": 0, "success": 0, "failure": 0}),
        "session_tokens_1d": defaultdict(lambda: {"tokens": 0, "calls": 0, "last_time": 0}),
        "token_trend_1d": Counter(),
        "pending_api_by_scene": {},
    }


def _stat_feed_lines(acc: Dict[str, Any], lines, one_day_ago: int) -> None:
    """把日志行喂给累加器。只做增量部分，不重扫历史。"""
    model_rank = acc["model_rank"]
    session_tokens_1d = acc["session_tokens_1d"]
    pending = acc["pending_api_by_scene"]
    for line in lines:
        # 廉价前置过滤：没有 [API] 标记的行三条正则都不可能命中
        if _API_MARK not in line:
            continue

        m = _API_REQ_RE.match(line)
        if m:
            ts = _parse_log_timestamp(m.group("time")) or 0
            scene = _pretty_scene(m.group("scene"))
            model = m.group("model").strip()
            acc["total_messages"] += 1
            if ts:
                acc["message_trend"][datetime.fromtimestamp(ts).strftime("%m-%d %H:00")] += 1
            if scene.startswith("私聊"):
                acc["message_scene"]["私聊"] += 1
            elif scene.startswith("群聊"):
                acc["message_scene"]["群聊"] += 1
            else:
                acc["message_scene"]["其他"] += 1
            item = {
                "time": m.group("time").strip(),
                "timestamp": ts,
                "scene": scene,
                "model": model,
                "host": m.group("host").strip(),
                "message_count": int(m.group("msg") or 0),
                "preview": m.group("preview").strip(),
                "status": "pending",
                "tokens": 0,
            }
            acc["api_request_history"].append(item)
            pending[scene] = item
            model_rank[model]["calls"] += 1
            continue

        m = _API_OK_RE.match(line)
        if m:
            ts = _parse_log_timestamp(m.group("time")) or 0
            scene = _pretty_scene(m.group("scene"))
            model = m.group("model").strip()
            tokens = int(m.group("tokens") or 0)
            stats = model_rank[model]
            stats["tokens"] += tokens
            stats["success"] += 1
            if ts >= one_day_ago:
                row = session_tokens_1d[scene or "unknown"]
                row["tokens"] += tokens
                row["calls"] += 1
                row["last_time"] = max(row["last_time"], ts)
                acc["token_trend_1d"][datetime.fromtimestamp(ts).strftime("%m-%d %H:00")] += tokens
            if scene in pending:
                pending[scene]["status"] = "success"
                pending[scene]["tokens"] = tokens
            continue

        m = _API_FAIL_RE.match(line)
        if m:
            scene = _pretty_scene(m.group("scene"))
            model_rank[m.group("model").strip()]["failure"] += 1
            if scene in pending:
                pending[scene]["status"] = "failed"


def _stat_read_incremental(one_day_ago: int) -> Dict[str, Any]:
    """增量读取日志并累加统计。

    首次（或日志换天/被截断/累加器过期）时全量回读一次尾部；之后只从上次读到的
    字节偏移继续往后读，正则只跑在新增的那几行上。低配机上 WebUI 每分钟刷一次
    统计，原实现每次都要重扫 12000 行、跑三万多次正则匹配，增量之后几乎为零。
    """
    path = _log_file()
    with _stat_inc_lock:
        st = _stat_inc_state
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0

        same_file = st["log_path"] == str(path)
        # 文件变小说明被轮转/清空过，之前的偏移量失效
        rewound = size < st["file_size"]
        # 累加器里的 24h 窗口数据会过期，超过 6 小时就重建一次，
        # 否则 message_trend / token_trend 会一直留着一天前的桶
        stale = (int(time.time()) - int(st["last_check"] or 0)) > 6 * 3600
        acc = st.get("_acc")

        if acc is None or not same_file or rewound or stale:
            acc = _stat_new_accumulator()
            # 全量回读：拿尾部 12000 行重建
            _stat_feed_lines(acc, _iter_runtime_log_lines(12000), one_day_ago)
            st["_acc"] = acc
            st["log_path"] = str(path)
            st["file_size"] = size
            st["last_check"] = int(time.time())
            return acc

        if size > st["file_size"]:
            # 只解码新增的那一段
            try:
                with open(path, "rb") as fp:
                    fp.seek(st["file_size"])
                    chunk = fp.read()
                text = chunk.decode("utf-8", errors="replace")
                # 末尾可能是半行（正在写入），留到下次；据此回退偏移量
                cut = text.rfind("\n")
                if cut < 0:
                    return acc  # 连一个完整行都没有，等下次
                consumed = len(text[: cut + 1].encode("utf-8", errors="replace"))
                _stat_feed_lines(acc, text[:cut].splitlines(), one_day_ago)
                st["file_size"] += consumed
            except OSError:
                pass
        st["last_check"] = int(time.time())
        return acc


def collect_statistics() -> Dict[str, Any]:
    # 每 60 秒最多重算一次：统计页会被前端轮询，不缓存会把 CPU 打满
    now = int(time.time())
    with _statistics_cache_lock:
        if _statistics_cache["data"] is not None and now - _statistics_cache["timestamp"] < 60:
            return _statistics_cache["data"]
    one_day_ago = now - 86400
    acc = _stat_read_incremental(one_day_ago)

    total_messages = acc["total_messages"]
    message_trend = acc["message_trend"]
    message_scene = acc["message_scene"]
    model_rank = acc["model_rank"]
    session_tokens_1d = acc["session_tokens_1d"]
    token_trend_1d = acc["token_trend_1d"]

    message_trend_list = [
        {"label": key, "value": message_trend[key]}
        for key in sorted(message_trend.keys())[-24:]
    ]
    token_trend_list = [
        {"label": key, "value": token_trend_1d[key]}
        for key in sorted(token_trend_1d.keys())[-24:]
    ]
    model_rank_list = sorted([
        {"model": model, **values}
        for model, values in model_rank.items()
    ], key=lambda x: (x.get("tokens", 0), x.get("calls", 0)), reverse=True)
    session_top10 = sorted([
        {
            "session": key,
            "tokens": values["tokens"],
            "calls": values["calls"],
            "last_time": values["last_time"],
        }
        for key, values in session_tokens_1d.items()
    ], key=lambda x: (x.get("tokens", 0), x.get("calls", 0)), reverse=True)[:10]
    # 累加器里的 history 会一直增长，这里只取最近 30 条给前端；
    # 同时把累加器内部也裁一下，避免长期运行后列表无限膨胀
    history = acc["api_request_history"]
    if len(history) > 200:
        del history[:-200]
    api_request_history = sorted(history, key=lambda x: x.get("timestamp", 0), reverse=True)[:30]

    total_api_calls = sum(item.get("calls", 0) for item in model_rank_list)
    total_api_tokens = sum(item.get("tokens", 0) for item in model_rank_list)

    result = {
        "summary": {
            "message_count": total_messages,
            "api_calls": total_api_calls,
            "api_tokens": total_api_tokens,
            "model_count": len(model_rank_list),
        },
        "message_scene": [
            {"label": label, "value": value}
            for label, value in message_scene.items()
        ],
        "message_trend": message_trend_list,
        "api_history": api_request_history,
        "model_ranking": model_rank_list,
        "token_trend_1d": token_trend_list,
        "session_tokens_top10_1d": session_top10,
        "generated_at": now,
        "has_data": bool(total_messages or total_api_calls),
    }
    with _statistics_cache_lock:
        _statistics_cache["data"] = result
        _statistics_cache["timestamp"] = now
    return result


from webui_core.versioning import compare_versions as _compare_versions
from webui_core.versioning import parse_version_parts as _parse_version_parts

def _set_update_install_status(state: str, text: str, detail: str = "") -> None:
    global _update_install_status
    _update_install_status = {
        "state": str(state or "idle"),
        "text": str(text or ""),
        "detail": str(detail or ""),
        "updated_at": int(time.time()),
    }


def _resolve_python_executable() -> str:
    """尽量解析出当前环境可用的 Python 可执行文件路径。"""
    candidates = []
    seen = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        # 排除空字符串或仅仅是一个空参数
        if not text or text in seen:
            return
        seen.add(text)
        candidates.append(text)

    if sys.executable and str(sys.executable).strip():
        _add(sys.executable)

    for arg in getattr(sys, "orig_argv", []) or []:
        if arg and not str(arg).startswith("-"):
            # 有时 orig_argv 里的 python 可能是命令名
            if "python" in str(arg).lower() or "py" == str(arg).lower():
                _add(arg)

    argv0 = str((sys.argv or [""])[0] or "").strip()
    if argv0 and not argv0.endswith(".py"):
        _add(argv0)

    for name in ("python3", "python", "py"):
        found = shutil.which(name)
        if found:
            _add(found)

    for candidate in candidates:
        # 排除空字符串
        if not candidate:
            continue
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found

    # 如果实在找不到，回退到当前环境的 "python"
    return "python"


def _copy_tree_contents(src: Path, dst: Path, skip_names: set[str] = None):
    if skip_names is None:
        skip_names = set()
    for item in src.iterdir():
        if item.name in skip_names:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_tree_contents(item, target, skip_names)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


UPDATE_SKIP_NAMES = {".git", ".github", "config_backup", "data", "temps", "Tools", "__pycache__", "my_bot.lock", "my_bot.pid", "update_backup"}


def _get_github_download_mirrors() -> list[str]:
    """GitHub 更新镜像前缀。空字符串表示直连。

    用户可在 WebUI / config.json 里配置：
      Others.github_download_mirrors = ["https://your-proxy.example/"]

    切换策略固定为：先直连 GitHub，失败后再按用户配置的镜像依次尝试。
    每个镜像会按 `prefix + github_url` 拼接，例如：
      https://your-proxy.example/https://github.com/{repo}/releases/latest
    """
    values = [""]
    try:
        cfg = read_json(CONFIG_PATH, {})
        raw = (cfg.get("Others") or {}).get("github_download_mirrors")
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.splitlines() if x.strip()]
        if isinstance(raw, list):
            for item in raw:
                text = str(item or "").strip()
                if not text:
                    continue
                if text.lower() in {"direct", "github", "直连"}:
                    text = ""
                elif not text.endswith("/"):
                    text += "/"
                if text not in values:
                    values.append(text)
    except Exception:
        pass
    return values


def _get_http_proxy() -> str:
    try:
        return str((read_json(CONFIG_PATH, {}).get("Others") or {}).get("http_proxy") or "").strip()
    except Exception:
        return ""

def _make_opener(proxy: str = ""):
    if not proxy:
        proxy = _get_http_proxy()
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _github_accelerated_urls(github_url: str) -> list[str]:
    result = []
    for prefix in _get_github_download_mirrors():
        url = github_url if not prefix else prefix + github_url
        if url not in result:
            result.append(url)
    return result


def _update_source_label(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc or "github.com"
        return "GitHub 直连" if host.lower() == "github.com" else host
    except Exception:
        return "未知更新源"


def _create_update_backup(version: str) -> Path:
    """更新前备份当前项目核心文件，失败时可回滚。"""
    safe_version = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(version or "unknown"))[:80]
    backup_root = BASE_DIR / "update_backup"
    backup_dir = backup_root / f"before_{safe_version}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    _copy_tree_contents(BASE_DIR, backup_dir, skip_names=UPDATE_SKIP_NAMES)
    return backup_dir


def _restore_update_backup(backup_dir: Path) -> None:
    """从更新备份回滚。为避免新版本残留文件干扰，先清理可覆盖区域，再拷回备份。"""
    if not backup_dir or not backup_dir.exists():
        return
    for item in BASE_DIR.iterdir():
        if item.name in UPDATE_SKIP_NAMES:
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except Exception as e:
            print(f"[更新回滚] 清理 {item} 失败，继续尝试覆盖：{e}")
    _copy_tree_contents(backup_dir, BASE_DIR, skip_names=set())


def set_pre_restart_callback(fn) -> None:
    """供外部注册：自动更新完成在拉起新进程之前会被调用一次，
    用来保存内存状态、释放外部资源等。失败不会中断重启流程。
    """
    global _pre_restart_callback
    _pre_restart_callback = fn


def set_qq_send_callback(fn) -> None:
    """供 main.py 注册通过当前 OneBot / Hyper 连接发消息的同步入口。"""
    global _qq_send_callback
    _qq_send_callback = fn


def set_debug_self_message_callback(fn) -> None:
    """供 main.py 注册调试用的“给机器人自己发消息”入口。"""
    global _debug_self_message_callback
    _debug_self_message_callback = fn



def set_chatroom_agent_callbacks(send_fn, stop_fn=None) -> None:
    """供 main.py 注册统一聊天室 Agent 入口与中断入口。"""
    global _chatroom_agent_callback, _chatroom_stop_callback
    _chatroom_agent_callback = send_fn
    _chatroom_stop_callback = stop_fn


def is_feature_enabled_now(key: str, default: bool = True) -> bool:
    """每次读取当前 config.json，使功能开关无需重启即可生效。"""
    raw = read_json(CONFIG_PATH, {}).get("FeatureSwitches", {})
    fallback = DEFAULT_FEATURE_SWITCHES.get(key, default)
    if isinstance(raw, dict) and key in raw:
        return normalize_bool_config(raw.get(key), default=fallback)
    return normalize_bool_config(fallback, default=default)


_trace_store_fallback = None


def _trace_store():
    """取 AI 追踪存储。

    正常部署下 WebUI 是 main.py 的线程，import __main__ 拿到的就是同一个
    TraceStore 单例，读写共享同一把锁。仅在独立运行 webui.py 时才懒建兜底实例，
    此时 Bot 没在跑，不存在并发写。
    """
    global _trace_store_fallback
    try:
        import __main__ as main_mod  # type: ignore
        store = getattr(main_mod, "trace_store", None)
        if store is not None:
            return store
    except Exception:
        pass
    if _trace_store_fallback is None:
        from bot.trace_store import create_trace_store
        _trace_store_fallback = create_trace_store(register_atexit=False)
    return _trace_store_fallback


def _restart_current_process_after_update() -> None:
    """自动更新完成后，拉起新进程并退出旧进程。

    要点：
      1) 启动新进程前调用 _pre_restart_callback，让旧进程先保存状态、释放外部资源；
      2) 旧进程退出前先 stop_webui / flush，并把等待时间放大到 1.8s 给新进程喘息；
      3) 保留原 close_fds，但显式 stdin=DEVNULL/stdout=stderr，避免句柄继承问题。
    """
    # 先执行外部收尾回调（仅旧进程一次）
    try:
        if callable(_pre_restart_callback):
            _pre_restart_callback()
    except Exception as e:
        print(f"[更新] pre_restart 回调失败（忽略继续）：{e}")

    # 再停 WebUI（释放端口，避免新旧进程抢同端口）
    try:
        stop_webui()
    except Exception:
        pass

    argv = []
    orig_argv = [str(x).strip() for x in (getattr(sys, "orig_argv", []) or []) if str(x or "").strip()]
    if orig_argv:
        argv = orig_argv
    else:
        python_exe = _resolve_python_executable()
        argv = [python_exe] + list(sys.argv)

    try:
        if os.name == "nt":
            # Windows 下继续继承当前控制台，更新后能直接在原窗口看到新进程日志。
            subprocess.Popen(
                argv,
                cwd=str(BASE_DIR),
                close_fds=False,
            )
        else:
            # Linux/macOS 下旧进程退出时，终端/SSH 可能给同一进程组发送 SIGHUP。
            # 新进程必须脱离旧会话，否则会出现“安装完成后窗口没了、服务没起来”。
            log_dir = BASE_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(os.devnull, "rb") as stdin, open(log_dir / "update-restart.log", "ab", buffering=0) as log_file:
                # 日志内容是新进程 stdout 的原样重定向，本身没有时间戳；
                # 这里在每次重启前写一行带时间的分隔头，方便区分每次重启的输出。
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                log_file.write(f"\n===== [{stamp}] 自动更新重启，启动新进程 =====\n".encode("utf-8"))
                subprocess.Popen(
                    argv,
                    cwd=str(BASE_DIR),
                    stdin=stdin,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
    except Exception as e:
        raise RuntimeError(f"启动新进程失败: {e}") from e

    def _exit_later():
        try:
            time.sleep(1.8)
        finally:
            os._exit(0)

    threading.Thread(target=_exit_later, name="XcBot-ExitAfterUpdate", daemon=True).start()


def install_latest_update() -> None:
    if not _update_install_lock.acquire(blocking=False):
        raise RuntimeError("已有更新任务正在执行")

    def _worker():
        update_backup_dir = None
        rollback_done = False
        try:
            info = fetch_update_info(force=True)
            latest_version = str(info.get("latest_version") or "").strip()
            tag_name = str(info.get("tag_name") or latest_version).strip()
            asset_zip_url = str(info.get("asset_zip_url") or "").strip()
            zip_url = str(info.get("zipball_url") or "").strip()
            if not latest_version:
                raise RuntimeError("未获取到可安装的更新包")

            _set_update_install_status("downloading", "正在下载更新", latest_version)
            with tempfile.TemporaryDirectory(prefix="xcbot_update_") as tmp:
                tmp_dir = Path(tmp)
                zip_path = tmp_dir / "update.zip"
                extract_dir = tmp_dir / "extract"
                old_config_copy = tmp_dir / "config-old.json"
                had_old_config = CONFIG_PATH.exists()

                download_candidates = []
                cfg_for_repo = read_json(CONFIG_PATH, {})
                repo = str((cfg_for_repo.get("Others") or {}).get("github_repo", "") or "").strip() or GITHUB_REPO
                raw_tag = urllib.parse.quote(tag_name)
                github_asset_url = asset_zip_url or (
                    f"https://github.com/{repo}/releases/download/{raw_tag}/XcBot.zip"
                    if repo and tag_name else ""
                )
                github_zip_url = zip_url or (f"https://github.com/{repo}/archive/refs/tags/{raw_tag}.zip" if repo and tag_name else "")
                # Release 附件是发布者实际上传的成品包；GitHub 自动生成的 Tag
                # 源码包可能与附件内容不同，因此附件必须优先，Tag 仅作后备。
                if github_asset_url:
                    download_candidates.extend(_github_accelerated_urls(github_asset_url))
                if github_zip_url:
                    for candidate in _github_accelerated_urls(github_zip_url):
                        if candidate not in download_candidates:
                            download_candidates.append(candidate)
                if not download_candidates:
                    raise RuntimeError("未获取到可下载的更新地址")

                last_error = None
                tried = []
                for candidate in download_candidates:
                    tried.append(candidate)
                    try:
                        _set_update_install_status("downloading", f"正在下载更新（{urllib.parse.urlparse(candidate).netloc or 'github.com'}）", latest_version)
                        req = urllib.request.Request(
                            candidate,
                            headers={
                                "User-Agent": "XcBot-WebUI/1.0",
                                "Accept": "*/*",
                            },
                        )
                        with urllib.request.urlopen(req, timeout=60) as resp, zip_path.open("wb") as f:
                            _read_capped_response(resp, sink=f)
                        last_error = None
                        print(f"[更新] 下载成功: {candidate}")
                        break
                    except Exception as download_error:
                        last_error = download_error
                        print(f"[更新] 下载失败，尝试下一个镜像: {candidate} -> {download_error}")
                if last_error is not None:
                    raise RuntimeError(f"所有下载源均失败，最后错误: {last_error}\n已尝试:\n" + "\n".join(tried))

                _set_update_install_status("extracting", "正在解压更新", latest_version)
                extract_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    _check_zip_safety(zf)
                    extract_root = extract_dir.resolve()
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        safe_rel = _safe_extract_relpath(info.filename)
                        if not safe_rel:
                            print(f"[更新] 跳过可疑路径: {info.filename}")
                            continue
                        out = (extract_root / safe_rel).resolve()
                        try:
                            out.relative_to(extract_root)
                        except ValueError:
                            print(f"[更新] 跳过越界路径: {info.filename}")
                            continue
                        out.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info, "r") as src, out.open("wb") as dst:
                            shutil.copyfileobj(src, dst)

                # Release 附件通常把 main.py 直接放在 ZIP 根目录；GitHub 自动
                # 生成的 Tag ZIP 则会额外套一层 "repo-tag" 目录。两种都支持，
                # 并用项目标志文件识别根目录，不能随便取第一个子目录。
                if (extract_dir / "main.py").is_file() and (extract_dir / "webui.py").is_file():
                    release_root = extract_dir
                else:
                    candidates = [
                        x for x in extract_dir.iterdir()
                        if x.is_dir() and (x / "main.py").is_file() and (x / "webui.py").is_file()
                    ]
                    if len(candidates) != 1:
                        raise RuntimeError(
                            "更新包解压失败：未找到唯一项目目录"
                            f"（候选数量 {len(candidates)}）"
                        )
                    release_root = candidates[0]

                if had_old_config:
                    shutil.copy2(CONFIG_PATH, old_config_copy)

                _set_update_install_status("backup", "正在备份当前版本", latest_version)
                update_backup_dir = _create_update_backup(latest_version)
                print(f"[更新] 当前版本已备份到: {update_backup_dir}")

                try:
                    _set_update_install_status("installing", "正在安装更新", latest_version)
                    _copy_tree_contents(release_root, BASE_DIR, skip_names=UPDATE_SKIP_NAMES)

                    if had_old_config and old_config_copy.exists() and CONFIG_PATH.exists():
                        _set_update_install_status("migrating", "正在迁移配置", latest_version)
                        from config_migrate import migrate as migrate_config
                        migrate_config(
                            str(old_config_copy),
                            str(CONFIG_PATH),
                            str(BASE_DIR / "config_backup"),
                            remove_old=True,
                        )
                    else:
                        print("[更新] 未找到旧版 config.json，已直接使用新版本自带 config.json。")

                    try:
                        cfg = read_json(CONFIG_PATH, {})
                        if not isinstance(cfg, dict):
                            cfg = {}
                        others = cfg.get("Others", {})
                        if not isinstance(others, dict):
                            others = {}
                            cfg["Others"] = others
                        others["version_name"] = latest_version
                        write_json(CONFIG_PATH, cfg)
                    except Exception as version_error:
                        print(f"[更新] 同步 config.json 版本号失败: {version_error}")

                    _set_update_install_status("dependencies", "正在安装依赖", latest_version)
                    python_exe = _resolve_python_executable()
                    subprocess.run(
                        [python_exe, "-m", "pip", "install", "-r", str(BASE_DIR / "requirements.txt"), "--disable-pip-version-check"],
                        cwd=str(BASE_DIR),
                        check=True,
                    )
                except Exception:
                    if update_backup_dir and update_backup_dir.exists():
                        _set_update_install_status("rollback", "更新失败，正在回滚", latest_version)
                        _restore_update_backup(update_backup_dir)
                        rollback_done = True
                        print(f"[更新回滚] 已从备份恢复: {update_backup_dir}")
                    raise

            _set_update_install_status("restarting", "安装完成，正在重启", latest_version)
            with _update_cache_lock:
                _update_cache["timestamp"] = 0.0
                _update_cache["data"] = None
            _restart_current_process_after_update()
        except Exception as e:
            detail = str(e)
            if rollback_done:
                detail = "更新失败，已自动回滚。" + detail
            elif update_backup_dir:
                detail = f"更新失败，备份保留在 {update_backup_dir}。" + detail
            _set_update_install_status("error", "更新失败", detail)
            print(f"自动更新失败: {detail}")
            traceback.print_exc()
        finally:
            _update_install_lock.release()

    threading.Thread(target=_worker, name="XcBot-AutoUpdate", daemon=True).start()


def _scrape_latest_tag_via_redirect(repo: str, timeout: float = 6.0) -> Tuple[str, str]:
    """不走 GitHub API，靠 `/releases/latest` 的 302 重定向拿 tag。

    支持 GitHub 加速/镜像：先直连，再按 Others.github_download_mirrors 配置的前缀尝试。
    例如 prefix=https://gh.llkk.cc/ 时访问：
      https://gh.llkk.cc/https://github.com/{repo}/releases/latest
    """
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    opener = urllib.request.build_opener(_NoRedirect)
    github_url = f"https://github.com/{repo}/releases/latest"
    last_error = None
    for url in _github_accelerated_urls(github_url):
        req = urllib.request.Request(url, headers={"User-Agent": "XcBot-WebUI/1.0"})
        final_url = ""
        try:
            with opener.open(req, timeout=timeout) as resp:
                final_url = resp.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                final_url = e.headers.get("Location", "") or ""
            else:
                last_error = e
                continue
        except Exception as e:
            last_error = e
            continue

        if not final_url:
            last_error = RuntimeError("未拿到 GitHub releases 重定向地址")
            continue

        # 兼容直连或镜像后 URL：.../releases/tag/<tag>
        m = re.search(r"/releases/tag/([^/?#]+)", final_url)
        if not m:
            last_error = RuntimeError(f"无法从重定向地址提取 tag：{final_url}")
            continue
        tag = urllib.parse.unquote(m.group(1))
        return tag, f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(tag)}"

    if last_error:
        raise last_error
    raise RuntimeError("未拿到 GitHub releases 重定向地址")


def _scrape_release_atom(repo: str, target_tag: str, timeout: float = 6.0) -> Dict[str, str]:
    """读 `/releases.atom`（公开 RSS feed），补全 release 标题/时间/正文。

    也会走可配置镜像，失败时返回空字段，不影响主流程。
    """
    out = {"release_name": "", "published_at": "", "body": ""}
    raw = ""
    github_url = f"https://github.com/{repo}/releases.atom"
    for url in _github_accelerated_urls(github_url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "XcBot-WebUI/1.0"})
            with _make_opener().open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            if raw:
                break
        except Exception:
            continue
    if not raw:
        return out

    # 用极简正则解析 entry，避免引入 XML 解析器及命名空间复杂度
    entries = re.findall(r"<entry>(.*?)</entry>", raw, flags=re.DOTALL)
    for entry in entries:
        id_match = re.search(r"<id>([^<]+)</id>", entry)
        if not id_match:
            continue
        # id 形如 tag:github.com,2008:Repository/12345/v1.2.3
        if not id_match.group(1).rstrip().endswith("/" + target_tag):
            continue
        title_match = re.search(r"<title>([^<]*)</title>", entry)
        updated_match = re.search(r"<updated>([^<]+)</updated>", entry)
        content_match = re.search(r"<content[^>]*>(.*?)</content>", entry, flags=re.DOTALL)
        if title_match:
            out["release_name"] = html.unescape(title_match.group(1).strip())
        if updated_match:
            out["published_at"] = updated_match.group(1).strip()
        if content_match:
            # content 是 HTML 片段，剥掉标签当纯文本展示（避免在 WebUI 里渲染 raw HTML）
            text = re.sub(r"<[^>]+>", "", content_match.group(1))
            out["body"] = html.unescape(text).strip()
        break
    return out


def get_cached_update_info() -> Dict[str, Any]:
    with _update_cache_lock:
        cached = _update_cache.get("data")
        if isinstance(cached, dict):
            return dict(cached)
    return dict(_UPDATE_UNKNOWN)


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except Exception:
        return 0


def _debug_runtime_snapshot() -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "generated_at": int(time.time()),
        "gc": {
            "counts": list(gc.get_count()),
            "thresholds": list(gc.get_threshold()),
            # ponytail: gc.get_objects() 在大进程里会分配几百MB临时列表，每3s触发一次会OOM，改为gc.get_count()之和估算
            "tracked_objects": sum(gc.get_count()),
        },
        "webui": {
            "log_buffer": _safe_len(_log_buffer),
            "update_cache": bool(_update_cache.get("data")),
            "update_cache_age_seconds": int(max(0, time.time() - float(_update_cache.get("timestamp") or 0))) if _update_cache.get("timestamp") else None,
            "server_alive": _server is not None,
            "host": get_webui_config().get("host"),
            "port": get_webui_config().get("port"),
            "has_access_token": bool(str(get_webui_config().get("access_token") or "").strip()),
        },
        # 前端读 debug.resource_usage；原先只放在 status 顶层导致调试页资源全是 0
        "resource_usage": _get_resource_usage(),
        "update_install": dict(_update_install_status),
        "runtime": {},
    }

    # —— 系统级资源（不依赖 bot 主模块）——
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        snapshot["system"] = {
            "memory_total_mb": round(vm.total / 1024 / 1024, 1),
            "memory_used_mb": round(vm.used / 1024 / 1024, 1),
            "memory_available_mb": round(vm.available / 1024 / 1024, 1),
            "memory_percent": round(float(vm.percent), 1),
            "boot_time": int(getattr(psutil, "boot_time", lambda: 0)() or 0),
        }
        try:
            du = psutil.disk_usage(str(BASE_DIR))
            snapshot["system"]["disk_total_gb"] = round(du.total / 1024 / 1024 / 1024, 2)
            snapshot["system"]["disk_used_gb"] = round(du.used / 1024 / 1024 / 1024, 2)
            snapshot["system"]["disk_free_gb"] = round(du.free / 1024 / 1024 / 1024, 2)
            snapshot["system"]["disk_percent"] = round(float(du.percent), 1)
        except Exception:
            pass
        try:
            proc = psutil.Process(os.getpid())
            snapshot["process"] = {
                "pid": proc.pid,
                "create_time": int(proc.create_time()),
                "status": str(proc.status()),
                "num_handles": int(proc.num_handles()) if hasattr(proc, "num_handles") else None,
                "cwd": str(proc.cwd()),
            }
            threads = []
            for t in proc.threads()[:30]:
                threads.append({"id": int(getattr(t, "id", 0) or 0), "user_time": round(float(getattr(t, "user_time", 0) or 0), 2), "system_time": round(float(getattr(t, "system_time", 0) or 0), 2)})
            snapshot["process"]["thread_samples"] = threads
            # 命名线程（Python 层）
            import threading as _threading
            snapshot["process"]["thread_names"] = [th.name for th in _threading.enumerate()][:40]
        except Exception:
            pass
    except Exception as e:
        snapshot["system_error"] = repr(e)

    # —— 本地目录体量（temps / data / plugins）——
    try:
        def _dir_stats(path: Path, max_files: int = 5000) -> Dict[str, Any]:
            if not path.exists():
                return {"exists": False, "files": 0, "size_mb": 0}
            files = 0
            size = 0
            for root, _, names in os.walk(path):
                for name in names:
                    files += 1
                    if files > max_files:
                        return {"exists": True, "files": files, "size_mb": round(size / 1024 / 1024, 2), "truncated": True}
                    try:
                        size += (Path(root) / name).stat().st_size
                    except Exception:
                        pass
            return {"exists": True, "files": files, "size_mb": round(size / 1024 / 1024, 2), "truncated": False}

        snapshot["paths"] = {
            "base_dir": str(BASE_DIR),
            "temps": _dir_stats(BASE_DIR / "temps"),
            "data": _dir_stats(BASE_DIR / "data"),
            "plugins": _dir_stats(PLUGIN_DIR),
            "config_exists": CONFIG_PATH.exists(),
            "config_size_kb": round(CONFIG_PATH.stat().st_size / 1024, 2) if CONFIG_PATH.exists() else 0,
        }
    except Exception as e:
        snapshot["paths_error"] = repr(e)

    # —— webui 自身的连接状态（不依赖 bot 进程）——
    try:
        snapshot["connection"] = dict(_connection_status)
    except Exception:
        pass

    try:
        import __main__ as main_mod  # type: ignore
        runtime: Dict[str, Any] = {}

        nickname_cache = getattr(main_mod, "nickname_cache", None)
        if nickname_cache is not None:
            runtime["nickname_cache"] = {
                "items": _safe_len(nickname_cache),
                "max": getattr(main_mod, "MAX_NICKNAME_CACHE", None),
            }

        chat_db = getattr(main_mod, "chat_db", None)
        if isinstance(chat_db, dict):
            group_rows = []
            total_history = 0
            for group_id, data in chat_db.items():
                history = data.get("history", []) if isinstance(data, dict) else []
                count = _safe_len(history)
                total_history += count
                group_rows.append({"group": str(group_id), "history": count, "tokens": data.get("token_counter", 0) if isinstance(data, dict) else 0})
            group_rows.sort(key=lambda r: r["history"], reverse=True)
            runtime["chat_db"] = {"groups": _safe_len(chat_db), "history_total": total_history, "top_groups": group_rows[:20]}

        token_stats = getattr(main_mod, "token_stats", None)
        if token_stats is not None:
            try:
                # 触发 24h 窗口裁剪，保证 total_tokens 等是滚动窗口值
                if hasattr(token_stats, "get_stats"):
                    token_stats.get_stats()
            except Exception:
                pass
            runtime["token_stats"] = {
                "total_tokens": getattr(token_stats, "total_tokens", 0),
                "sessions": _safe_len(getattr(token_stats, "session_tokens", {})),
                "users": _safe_len(getattr(token_stats, "user_tokens", {})),
                "groups": _safe_len(getattr(token_stats, "group_tokens", {})),
                "detail_sessions": _safe_len(getattr(token_stats, "detailed_stats", {})),
                "window_hours": 24,
            }
            # Top sessions by tokens if available
            try:
                session_tokens = getattr(token_stats, "session_tokens", {}) or {}
                if isinstance(session_tokens, dict) and session_tokens:
                    top = sorted(
                        [{"session": str(k), "tokens": int(v or 0)} for k, v in session_tokens.items()],
                        key=lambda x: x["tokens"],
                        reverse=True,
                    )[:15]
                    runtime["token_stats"]["top_sessions"] = top
            except Exception:
                pass

        # —— 增强上下文管理器 cmc（当前真实使用的上下文）——
        cmc = getattr(main_mod, "cmc", None)
        if cmc is not None:
            groups = getattr(cmc, "groups", {}) or {}
            privates = getattr(cmc, "private_chats", {}) or {}
            def _ctx_msgs(c):
                try:
                    fn = getattr(c, "get_message_count", None)
                    if callable(fn):
                        return int(fn())
                    return _safe_len(getattr(c, "history", []))
                except Exception:
                    return 0
            client_pool = 0
            top_ctx = []
            for kind, mapping in (("group", groups), ("private", privates)):
                for sid, c in list(mapping.items())[:200]:
                    msgs = _ctx_msgs(c)
                    client_pool += _safe_len(getattr(c, "_client_pool", {}))
                    top_ctx.append({
                        "kind": kind,
                        "id": str(sid),
                        "messages": msgs,
                        "tokens": int(getattr(c, "total_tokens", 0) or 0),
                        "calls": int(getattr(c, "total_calls", 0) or 0),
                    })
            top_ctx.sort(key=lambda x: (x["messages"], x["tokens"]), reverse=True)
            ctx_info = {
                "group_contexts": _safe_len(groups),
                "private_contexts": _safe_len(privates),
                "loaded_messages": sum(item["messages"] for item in top_ctx),
                "client_pool": client_pool,
                "top_contexts": top_ctx[:15],
            }
            compressor = getattr(cmc, "compressor", None)
            if compressor is not None:
                try:
                    cstat = compressor.get_compression_stats() if hasattr(compressor, "get_compression_stats") else {}
                except Exception:
                    cstat = {}
                ctx_info["compression"] = {
                    "total_sessions": cstat.get("total_sessions", _safe_len(getattr(compressor, "compression_count", {}))),
                    "total_compressions": cstat.get("total_compressions", 0),
                    "threshold": cstat.get("threshold", getattr(compressor, "compression_threshold", None)),
                    "keep_recent": cstat.get("keep_recent", getattr(compressor, "keep_recent", None)),
                    "client_pool": _safe_len(getattr(compressor, "_client_pool", {})),
                }
            runtime["cmc"] = ctx_info

        # —— 已落盘 AI 记忆 ——
        chat_memory = getattr(main_mod, "chat_memory", None)
        if chat_memory is not None and hasattr(chat_memory, "get_all_sessions"):
            try:
                sess = chat_memory.get_all_sessions() or {}
                runtime["ai_memory"] = {
                    "private": _safe_len(sess.get("private", [])),
                    "group": _safe_len(sess.get("group", [])),
                }
            except Exception:
                pass

        # —— 功能热开关 ——
        get_fs = getattr(main_mod, "get_feature_switches", None)
        if callable(get_fs):
            try:
                fs = get_fs() or {}
                runtime["feature_switches"] = {str(k): bool(v) for k, v in fs.items()}
            except Exception:
                pass
        else:
            # 回退读配置
            try:
                runtime["feature_switches"] = collect_config_bundle().get("feature_switches", {})
            except Exception:
                pass

        # —— API / Key 状态（脱敏，由 key_manager 返回）——
        km = getattr(main_mod, "key_manager", None)
        if km is not None:
            api: Dict[str, Any] = {}
            try:
                if hasattr(km, "get_status_list"):
                    status_list = km.get_status_list() or []
                    now_ts = time.time()
                    kl = getattr(km, "key_list", []) or []
                    active = cooldown = disabled = multimodal = fails = 0
                    error_items = []
                    for it in kl:
                        if not isinstance(it, dict):
                            continue
                        if it.get("disabled"):
                            disabled += 1
                        elif float(it.get("cooldown_until", 0) or 0) > now_ts:
                            cooldown += 1
                        else:
                            active += 1
                        if it.get("supports_multimodal"):
                            multimodal += 1
                        fails += int(it.get("fail_count", 0) or 0)
                        if it.get("last_error"):
                            error_items.append({
                                "model": it.get("display_model") or it.get("model") or "",
                                "error": str(it.get("last_error") or "")[:200],
                                "fail_count": int(it.get("fail_count", 0) or 0),
                            })
                    slots = getattr(km, "model_slots", []) or []
                    switch_logs = []
                    if hasattr(km, "get_switch_logs"):
                        try:
                            switch_logs = km.get_switch_logs(15) or []
                        except Exception:
                            switch_logs = list(getattr(km, "switch_logs", []) or [])[-15:]
                    api = {
                        "total": _safe_len(status_list),
                        "active": active,
                        "cooldown": cooldown,
                        "disabled": disabled,
                        "multimodal": multimodal,
                        "fail_total": fails,
                        "model_slots": _safe_len(slots),
                        "current": km.get_current_display() if hasattr(km, "get_current_display") else "",
                        "default": km.get_default_display() if hasattr(km, "get_default_display") else "",
                        "switch_logs_count": _safe_len(getattr(km, "switch_logs", [])),
                        "switch_logs": switch_logs,
                        "recent_errors": error_items[:12],
                        "items": status_list,
                    }
            except Exception as e:
                api = {"error": repr(e)}
            runtime["api_keys"] = api

        # —— 插件 ——
        loaded = getattr(main_mod, "loaded_plugins", None)
        if loaded is not None:
            runtime["plugins"] = {
                "loaded": _safe_len(loaded),
                "disabled": _safe_len(getattr(main_mod, "disabled_plugins", [])),
                "failed": _safe_len(getattr(main_mod, "failed_plugins", [])),
                "modules": _safe_len(getattr(main_mod, "plugins", [])),
                "loaded_names": [str(x) for x in list(loaded)[:50]],
                "disabled_names": [str(x) for x in list(getattr(main_mod, "disabled_plugins", []) or [])[:50]],
                "failed_names": [str(x) for x in list(getattr(main_mod, "failed_plugins", []) or [])[:50]],
            }

        # —— 权限名单 ——
        perm: Dict[str, Any] = {
            "admin": _safe_len(getattr(main_mod, "ROOT_User", [])),
            "admin_preview": [str(x) for x in list(getattr(main_mod, "ROOT_User", []) or [])[:20]],
        }
        get_bl = getattr(main_mod, "get_all_blacklist", None)
        if callable(get_bl):
            try:
                bl = get_bl()
                perm["blacklist"] = _safe_len(bl)
                if isinstance(bl, (list, tuple, set)):
                    perm["blacklist_preview"] = [str(x) for x in list(bl)[:20]]
            except Exception:
                pass
        runtime["permissions"] = perm

        # —— 连接快照（bot 进程内）——
        conn_snap = getattr(main_mod, "RUNTIME_CONNECTION_SNAPSHOT", None)
        if isinstance(conn_snap, dict):
            runtime["connection_snapshot"] = {str(k): str(v) for k, v in conn_snap.items()}
        hot = getattr(main_mod, "HOT_SWITCH_IN_PROGRESS", None)
        if hot is not None and hasattr(hot, "is_set"):
            try:
                runtime["hot_switch"] = bool(hot.is_set())
            except Exception:
                pass

        # —— 缓存 / 冷却 / 运行时长 ——
        runtime["counters"] = {
            "poke_cooldowns": _safe_len(getattr(main_mod, "poke_cooldowns", {})),
            "summary_groups": _safe_len(getattr(main_mod, "daily_summary_records", {})),
            "generating": bool(getattr(main_mod, "generating", False)),
            "running": bool(getattr(main_mod, "running", False)),
            "bot_name": str(getattr(main_mod, "bot_name", "") or ""),
            "version_name": str(getattr(main_mod, "version_name", "") or ""),
            "reminder": str(getattr(main_mod, "reminder", "") or ""),
        }
        second_start = getattr(main_mod, "second_start", None)
        if isinstance(second_start, (int, float)):
            runtime["counters"]["uptime_seconds"] = int(time.time() - second_start)

        # —— 聊天室会话（WebUI chatroom 落盘）——
        try:
            chat_dir = CHATROOM_DIR
            if chat_dir.exists():
                sessions = [p for p in chat_dir.glob("*.json") if p.is_file()]
                runtime["webui_chat"] = {
                    "sessions": len(sessions),
                    "size_mb": round(sum(p.stat().st_size for p in sessions) / 1024 / 1024, 2),
                }
        except Exception:
            pass

        snapshot["runtime"] = runtime
    except Exception as e:
        snapshot["runtime_error"] = repr(e)

    return snapshot


def fetch_update_info(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    with _update_cache_lock:
        cached = _update_cache.get("data")
        if not force and cached and (now - float(_update_cache.get("timestamp") or 0)) < 600:
            return dict(cached)

    cfg = read_json(CONFIG_PATH, {})
    others_cfg = cfg.get("Others") or {}
    current_version = str(others_cfg.get("version_name", "") or "").strip()
    repo = str(others_cfg.get("github_repo", "") or "").strip() or GITHUB_REPO

    html_url = f"https://github.com/{repo}/releases/latest" if repo else ""
    result = {
        "repo": repo,
        "current_version": current_version,
        "tag_name": "",
        "latest_version": "",
        "has_update": False,
        "status": "unknown",
        "message": "暂未检查更新",
        "release_name": "",
        "published_at": "",
        "release_url": html_url,
        "asset_zip_url": "",
        "zipball_url": "",
        "body": "",
        "update_sources": _github_accelerated_urls(html_url) if html_url else [],
        "update_source": "",
    }
    if not repo:
        result.update({
            "status": "unknown",
            "message": "未配置 GitHub 仓库（Others.github_repo），已跳过在线检查。",
        })
        with _update_cache_lock:
            _update_cache["timestamp"] = now
            _update_cache["data"] = dict(result)
        return result

    # —— 直接扒 GitHub 网页路径，不走 api.github.com，无 60 次/小时限制 ——
    err_msg = ""
    tag_name = ""
    release_url = html_url
    try:
        tag_name, release_url = _scrape_latest_tag_via_redirect(repo)
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        if code == 404:
            err_msg = f"HTTP 404（未找到仓库 {repo} 或它没有 releases，请确认 Others.github_repo）"
        else:
            err_msg = f"HTTP {code}"
    except Exception as e:
        err_msg = str(e)

    if not tag_name:
        result.update({"status": "error", "message": f"获取更新失败：{err_msg or '未知错误'}"})
        with _update_cache_lock:
            _update_cache["timestamp"] = now
            _update_cache["data"] = dict(result)
        return result

    latest_version = tag_name
    # zip 下载地址不依赖 API，按 GitHub 固定 URL 规则拼即可
    asset_zip_url = f"https://github.com/{repo}/releases/download/{urllib.parse.quote(tag_name)}/XcBot.zip"
    zipball_url = f"https://github.com/{repo}/archive/refs/tags/{urllib.parse.quote(tag_name)}.zip"

    # 用 atom feed 补一下名字/时间/正文。失败也无所谓，不阻断。
    extra = _scrape_release_atom(repo, tag_name)
    release_name = extra.get("release_name") or latest_version
    published_at = extra.get("published_at") or ""
    body = extra.get("body") or ""

    compare = _compare_versions(current_version, latest_version) if current_version and latest_version else 0
    has_update = bool(current_version and latest_version and compare < 0)
    if has_update:
        status = "outdated"
        message = f"发现新版本：{latest_version}"
    elif current_version and latest_version:
        status = "latest"
        message = "当前已是最新版本"
    else:
        status = "unknown"
        message = "已获取发布信息，但当前版本号为空"

    result.update({
        "latest_version": latest_version,
        "tag_name": tag_name,
        "has_update": has_update,
        "status": status,
        "message": message,
        "release_name": release_name,
        "published_at": published_at,
        "release_url": release_url,
        "asset_zip_url": asset_zip_url,
        "zipball_url": zipball_url,
        "body": body,
    })

    with _update_cache_lock:
        _update_cache["timestamp"] = now
        _update_cache["data"] = dict(result)
    return result


def _store_registry() -> list:
    """下载仓库 zip，从每个插件的 metadata.yaml 构建列表，合并本地已安装插件"""
    zip_url = f"https://github.com/{PLUGIN_STORE_REPO}/archive/refs/heads/main.zip"
    raw = None
    for url in _github_accelerated_urls(zip_url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "XcBot-WebUI"})
            with _make_opener().open(req, timeout=20) as r:
                raw = _read_capped_response(r)
            break
        except Exception:
            pass
    data = []
    if raw:
        repo_short = PLUGIN_STORE_REPO.split("/")[-1]
        prefix = f"{repo_short}-main/plugins/"
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            _check_zip_safety(zf)
            for m in zf.namelist():
                if not m.endswith("/metadata.yaml") or not m.startswith(prefix):
                    continue
                parts = m[len(prefix):].split("/")
                if len(parts) != 2:
                    continue
                plugin_name = parts[0]
                meta = {"name": plugin_name, "version": "?", "description": "", "author": "-", "path": f"plugins/{plugin_name}", "entry": "setup.py"}
                for line in zf.read(m).decode("utf-8").splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip()] = v.strip()
                meta["installed"] = (PLUGIN_DIR / plugin_name).exists()
                data.append(meta)
    names_in_registry = {item["name"] for item in data}
    if PLUGIN_DIR.exists():
        for d in sorted(PLUGIN_DIR.iterdir()):
            if d.is_dir() and d.name not in names_in_registry:
                meta = {"name": d.name, "version": "?", "description": "本地插件", "author": "-", "path": "", "installed": True, "local_only": True}
                mf = d / "metadata.yaml"
                if mf.exists():
                    try:
                        for line in mf.read_text(encoding="utf-8").splitlines():
                            if ":" in line:
                                k, _, v = line.partition(":")
                                meta[k.strip()] = v.strip()
                    except Exception:
                        pass
                data.append(meta)
    return data


def _safe_extract_relpath(member: str) -> Optional[str]:
    """规范化 zip 成员相对路径；含穿越/绝对路径时返回 None。"""
    raw = str(member or "").replace("\\", "/").strip()
    if not raw or raw.endswith("/"):
        return None
    # zip 内绝对路径或盘符
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None
    parts = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


# 解压防护上限。压缩率极高的「zip 炸弹」用几十 KB 的包就能解出几十 GB，
# 只限制压缩包本身的字节数是不够的。
ZIP_MAX_MEMBERS = 3000
ZIP_MAX_MEMBER_BYTES = 32 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 256 * 1024 * 1024


# 网络下载压缩包的字节上限。_check_zip_safety 只能在包已经下载完之后检查，
# 所以下载阶段必须自己限量——否则一个几 GB 的响应在检查前就把磁盘/内存吃满。
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024


def _read_capped_response(resp, limit: int = MAX_DOWNLOAD_BYTES, sink=None) -> bytes:
    """按上限读取 HTTP 响应。sink 非空时边读边写文件，返回空 bytes。

    先看 Content-Length 提前拒绝，再边读边累计——只信声明长度是不够的，
    分块传输根本不带这个头。
    """
    declared = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
    if declared and str(declared).isdigit() and int(declared) > limit:
        raise ValueError(f"下载内容过大（声明 {declared} 字节，上限 {limit}）")
    total = 0
    chunks = []
    while True:
        chunk = resp.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"下载内容超过上限（{limit} 字节），已中断")
        if sink is not None:
            sink.write(chunk)
        else:
            chunks.append(chunk)
    return b"" if sink is not None else b"".join(chunks)


def _check_zip_safety(zf, *, max_members: int = ZIP_MAX_MEMBERS,
                      max_member: int = ZIP_MAX_MEMBER_BYTES,
                      max_total: int = ZIP_MAX_TOTAL_BYTES) -> None:
    """按成员数、单文件解压大小和解压总量校验 zip，超限直接抛错。

    用 infolist 里的 file_size 判断，不用真的解压——那正是炸弹想要的。
    """
    infos = zf.infolist()
    if len(infos) > max_members:
        raise ValueError(f"压缩包内文件过多（{len(infos)} 个，上限 {max_members}）")
    total = 0
    for info in infos:
        size = int(getattr(info, "file_size", 0) or 0)
        if size > max_member:
            raise ValueError(
                f"压缩包内单个文件过大（{info.filename} 解压后 {size} 字节，"
                f"上限 {max_member}）"
            )
        total += size
        if total > max_total:
            raise ValueError(f"压缩包解压后总大小超过上限（{max_total} 字节）")


def _safe_write_zip_member(dest_root: Path, rel: str, data: bytes) -> bool:
    """把 zip 成员写到 dest_root 下；越界则跳过。"""
    safe_rel = _safe_extract_relpath(rel)
    if not safe_rel:
        return False
    dest_root = dest_root.resolve()
    out = (dest_root / safe_rel).resolve()
    try:
        out.relative_to(dest_root)
    except ValueError:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return True


def _store_install(name: str, path: str) -> str:
    """下载并解压单个插件到 plugins/ 目录，返回安装结果描述"""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("无效的插件名")
    path = str(path or "").replace("\\", "/").strip().strip("/")
    if not path or ".." in path.split("/") or path.startswith("/"):
        raise ValueError("无效的插件路径")
    zip_url = f"https://github.com/{PLUGIN_STORE_REPO}/archive/refs/heads/main.zip"
    urls = _github_accelerated_urls(zip_url)
    last_err = ""
    data = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "XcBot-WebUI"})
            with _make_opener().open(req, timeout=30) as r:
                data = _read_capped_response(r)
            break
        except Exception as e:
            last_err = str(e)
            data = None
    if not data:
        raise RuntimeError(f"下载插件仓库失败：{last_err}")

    PLUGIN_DIR.mkdir(exist_ok=True)
    repo_short = PLUGIN_STORE_REPO.split("/")[-1]
    prefix = f"{repo_short}-main/{path}/"
    dest = PLUGIN_DIR / name
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        _check_zip_safety(zf)
        members = [m for m in zf.namelist() if m.startswith(prefix) and not m.endswith("/")]
        if not members:
            raise RuntimeError(f"zip 中找不到路径 {prefix}")
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        written = 0
        for m in members:
            rel = m[len(prefix):]
            if _safe_write_zip_member(dest, rel, zf.read(m)):
                written += 1
        if written <= 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError("插件解压失败：无有效文件或路径不合法")

    # 尝试通知 main 重载插件
    try:
        import __main__ as main_mod  # type: ignore
        reload_fn = getattr(main_mod, "load_plugins", None)
        if callable(reload_fn):
            main_mod.plugins = reload_fn()
    except Exception:
        pass
    return f"插件 {name} 已安装到 plugins/{name}，如未自动重载请发送 /重载插件"


def get_status() -> Dict[str, Any]:
    cfg = read_json(CONFIG_PATH, {})
    connection_cfg = cfg.get("Connection", {}) if isinstance(cfg.get("Connection", {}), dict) else {}
    return {
        "project": (cfg.get("Others") or {}).get("project_name", "XcBot"),
        "version": (cfg.get("Others") or {}).get("version_name", ""),
        "bot_name": (cfg.get("Others") or {}).get("bot_name", ""),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "python": sys.executable,
        "python_version": sys.version,
        "platform": platform.platform(),
        "argv": sys.argv,
        "uptime_seconds": int(time.time() - _started_at),
        "uptime_text": _format_uptime(int(time.time() - _started_at)),
        "resource_usage": _get_resource_usage(),
        "webui": get_webui_config(),
        "connection": {
            "protocol": cfg.get("protocol", "OneBot"),
            "mode": connection_cfg.get("mode", ""),
            "host": connection_cfg.get("host", ""),
            "port": connection_cfg.get("port", ""),
            "listener_host": connection_cfg.get("listener_host", ""),
            "listener_port": connection_cfg.get("listener_port", ""),
        },
        "update": get_cached_update_info(),
        "update_install": dict(_update_install_status),
        "connection_status": dict(_connection_status),
        "feature_switches": collect_config_bundle().get("feature_switches", {}),
        "debug": _debug_runtime_snapshot(),
    }


def _json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200):
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass


def _text_response(handler: BaseHTTPRequestHandler, text: str, content_type="text/html; charset=utf-8", status: int = 200, cache: str = "no-store"):
    body = text.encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", cache)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass


BACKGROUND_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                   ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
# 上传的背景图统一存这里。文件名带内容哈希，换图就是换文件名。
BACKGROUND_UPLOAD_DIR = BASE_DIR / "data" / "webui_bg"
MAX_BACKGROUND_BYTES = 12 * 1024 * 1024


def save_uploaded_background(filename: str, raw: bytes) -> str:
    """把浏览器上传的图片存到服务器，返回写进配置的相对路径。

    机器人常跑在远程服务器上，用户通过端口转发访问 WebUI。那种情况下
    在「自定义背景图片」里填本机路径是找不到文件的——服务端按那个路径去
    自己的磁盘上找。所以要支持把图片真正传上来。

    文件名带内容哈希（bg_<hash>.png）。固定叫 background.png 的话，
    换图后 URL 一模一样，浏览器会继续用缓存里的旧图——重启程序、
    刷新页面都没用，因为缓存在浏览器那边。名字变了 URL 就变了，
    这件事不依赖任何缓存头是否被正确遵守。
    """
    ext = Path(str(filename or "")).suffix.lower()
    if ext not in BACKGROUND_EXTS:
        raise ValueError("只支持 jpg / png / webp / gif / bmp")
    if not raw:
        raise ValueError("图片内容为空")
    if len(raw) > MAX_BACKGROUND_BYTES:
        raise ValueError(f"图片过大（上限 {MAX_BACKGROUND_BYTES // 1048576}MB）")
    BACKGROUND_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    target = BACKGROUND_UPLOAD_DIR / f"bg_{digest}{ext}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, target)
    # 换图后清掉其他的：只保留当前这张，目录不会越堆越多。
    # 放在写入成功之后做，中途失败时旧图还在，背景不会突然消失。
    for old in BACKGROUND_UPLOAD_DIR.iterdir():
        if old != target and old.is_file() and old.name.lower().startswith(("bg_", "background.")):
            try:
                old.unlink()
            except OSError:
                pass
    return str(target.relative_to(BASE_DIR)).replace("\\", "/")


def _configured_background_image_response(handler: BaseHTTPRequestHandler):
    parsed = urllib.parse.urlparse(handler.path)
    qs = urllib.parse.parse_qs(parsed.query)
    path_text = str((qs.get("v") or [""])[0] or "").strip()
    if not path_text:
        path_text = str((read_json(CONFIG_PATH, {}).get("WebUI") or {}).get("background_image", "") or "").strip()
    if not path_text or re.match(r"^https?://", path_text, flags=re.I):
        _json_response(handler, {"ok": False, "error": "未配置本地背景图片"}, 404)
        return
    path = Path(path_text)
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        resolved = path.resolve()
    except Exception:
        _json_response(handler, {"ok": False, "error": "背景图片路径无效"}, 400)
        return
    if not resolved.exists() or not resolved.is_file() or resolved.suffix.lower() not in BACKGROUND_EXTS:
        _json_response(handler, {"ok": False, "error": "背景图片不存在或格式不支持"}, 404)
        return
    content_type = BACKGROUND_EXTS.get(resolved.suffix.lower(), "application/octet-stream")
    # 文件名带内容哈希，换图必然换 URL，所以可以放心让浏览器长期缓存。
    _binary_response(handler, resolved.read_bytes(), content_type,
                     cache="public, max-age=31536000, immutable")


def _binary_response(handler: BaseHTTPRequestHandler, body: bytes, content_type="application/octet-stream",
                     status: int = 200, cache: str = "public, max-age=3600"):
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", cache)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass


def _is_loopback_host(host: str) -> bool:
    """判断监听地址是否只有本机可达。

    空字符串和 0.0.0.0 / :: 都表示「所有网卡」，必须算作非回环。
    """
    text = str(host or "").strip().strip("[]")
    if not text:
        return False
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        # 域名无法判断，保守起见按非回环处理
        return False


def _token_equal(given: str, expected: str) -> bool:
    """恒定时间比较访问 Token。

    不能用 == ：str.__eq__ 在首个不同字符处就返回，攻击者可以逐字符测量响应
    时间把 Token 还原出来。compare_digest 要求两侧同为 bytes 且非 ASCII 会抛
    TypeError，所以统一先 encode。
    """
    try:
        return hmac.compare_digest(
            str(given or "").encode("utf-8"), str(expected or "").encode("utf-8")
        )
    except Exception:
        return False


class WebUIHandler(BaseHTTPRequestHandler):
    server_version = "XcBotWebUI/1.0"

    # 允许用 ?token= 传凭据的白名单。query string 会进浏览器历史、Referer、
    # 反代 access log，所以只留给必须直接嵌在 <img src> / <a href> 里的资源，
    # 其余接口一律只认 X-WebUI-Token 头。
    QUERY_TOKEN_PATHS = frozenset({"/api/webui/background", "/api/raw-log"})

    def log_message(self, fmt, *args):
        return

    def _auth_ok(self) -> bool:
        token = str(get_webui_config().get("access_token", "") or "")
        if not token:
            return True
        parsed = urllib.parse.urlparse(self.path)
        # 用 compare_digest 而不是 ==：str.__eq__ 首字符不同就立即返回，
        # 攻击者能逐字符测量响应时间把 Token 猜出来
        if _token_equal(self.headers.get("X-WebUI-Token", ""), token):
            return True
        if parsed.path in self.QUERY_TOKEN_PATHS:
            qs = urllib.parse.parse_qs(parsed.query)
            return _token_equal((qs.get("token") or [""])[0], token)
        return False

    def _read_body_json(self, max_bytes: int = 20 * 1024 * 1024) -> Tuple[Optional[Any], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            # 防止超大 body 撑爆内存（聊天附件/插件上传已另有业务上限）
            if length < 0 or length > max_bytes:
                self._discard_body(length)
                return None, "请求体过大"
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            return json.loads(raw), None
        except Exception as e:
            return None, str(e)

    def _discard_body(self, length: int, chunk: int = 64 * 1024):
        """丢弃超限请求体，避免直接关连接导致客户端收不到 413。"""
        remaining = min(max(length, 0), 256 * 1024 * 1024)
        try:
            while remaining > 0:
                data = self.rfile.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
        except Exception:
            pass

    def _guard(self) -> bool:
        # 必须用解析后的 path 判断，不能用原始 self.path：客户端可以发
        # absolute-form 请求目标（GET http://host:port/api/config HTTP/1.1），
        # 原始字符串不以 /api/ 开头就跳过了鉴权，而后面的路由用的是 parsed.path，
        # 照样命中 /api/config —— 鉴权被完全绕过。
        raw = str(self.path or "")
        # 顺手拒掉 absolute-form：这是给正向代理用的形式，直连服务端没有正当用途
        if "://" in raw.split("?", 1)[0]:
            _json_response(self, {"ok": False, "error": "不支持的请求目标形式"}, 400)
            return False
        try:
            path = urllib.parse.urlparse(raw).path or ""
        except Exception:
            _json_response(self, {"ok": False, "error": "请求路径无法解析"}, 400)
            return False
        if path.startswith("/api/") and not self._auth_ok():
            _json_response(self, {"ok": False, "error": "未授权：访问 Token 不正确或已失效", "login": "/auth/login"}, 401)
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                _text_response(self, INDEX_HTML)
            elif parsed.path == "/auth/login":
                _text_response(self, LOGIN_HTML)
            elif parsed.path in ("/assets/icon.jpg", "/favicon.ico"):
                if BOT_ICON_PATH.exists():
                    _binary_response(self, BOT_ICON_PATH.read_bytes(), "image/jpeg")
                else:
                    _json_response(self, {"ok": False, "error": "Icon Not Found"}, 404)
            elif parsed.path.startswith("/static/"):
                fname = parsed.path[len("/static/"):]
                cached = _STATIC_ASSET_CONTENT.get(fname)
                fpath = (BASE_DIR / "static" / fname).resolve()
                if cached is not None:
                    ct = "text/javascript" if fname.endswith(".js") else "text/css"
                    _text_response(self, cached, ct + "; charset=utf-8", cache="public, max-age=31536000, immutable")
                elif fpath.is_file() and str(fpath).startswith(str((BASE_DIR / "static").resolve())):
                    ct = "text/javascript" if fname.endswith(".js") else "text/css"
                    _text_response(self, fpath.read_text(encoding="utf-8"), ct + "; charset=utf-8", cache="public, max-age=31536000, immutable")
                else:
                    _json_response(self, {"ok": False, "error": "Not Found"}, 404)
            elif parsed.path == "/api/webui/background":
                _configured_background_image_response(self)
            elif parsed.path == "/api/status":
                _json_response(self, {"ok": True, "data": get_status()})
            elif parsed.path == "/api/auth/state":
                # 无需鉴权（token 为空时本来就全放行；设了 token 后此接口已被
                # _guard 拦住，只有已登录才能查）——前端靠它决定是否弹设置窗口
                _json_response(self, {"ok": True, "data": {
                    "has_token": bool(str(get_webui_config().get("access_token", "") or "").strip()),
                    "host": str(get_webui_config().get("host", "")),
                    "loopback_only": _is_loopback_host(get_webui_config().get("host", "")),
                }})
            elif parsed.path == "/api/config":
                _json_response(self, {"ok": True, "data": collect_config_bundle()})
            elif parsed.path == "/api/logs":
                qs = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = max(1, min(int((qs.get("limit") or ["100"])[0]), 5000))
                except (TypeError, ValueError):
                    limit = 100
                _json_response(self, {"ok": True, "data": get_recent_logs(limit)})
            elif parsed.path == "/api/features":
                bundle = collect_config_bundle()
                _json_response(self, {"ok": True, "data": {"feature_switches": bundle.get("feature_switches", {}), "feature_meta": bundle.get("feature_meta", FEATURE_META)}})
            elif parsed.path == "/api/agent/mcp":
                _json_response(self, {"ok": True, "data": get_mcp_state()})
            elif parsed.path == "/api/ui-state":
                qs = urllib.parse.parse_qs(parsed.query)
                try:
                    log_limit = max(1, min(int((qs.get("log_limit") or ["100"])[0]), 5000))
                except (TypeError, ValueError):
                    log_limit = 100
                _json_response(self, {"ok": True, "data": collect_ui_state(log_limit=log_limit)})
            elif parsed.path == "/api/statistics":
                _json_response(self, {"ok": True, "data": collect_statistics()})
            elif parsed.path == "/api/update/check":
                _json_response(self, {"ok": True, "data": fetch_update_info(force=True)})
            elif parsed.path == "/api/debug/test-log":
                qs = urllib.parse.parse_qs(parsed.query)
                level = qs.get("level", ["info"])[0]
                if level == "error":
                    print("[TEST] \x1b[31mERROR 测试日志：这是一条错误日志\x1b[0m")
                elif level == "warn":
                    print("[TEST] \x1b[33mWARN 测试日志：这是一条警告日志\x1b[0m")
                else:
                    print("[TEST] INFO 测试日志：这是一条普通日志")
                _json_response(self, {"ok": True})
            elif parsed.path == "/api/raw-log":
                text = _log_file().read_text(encoding="utf-8", errors="replace") if _log_file().exists() else ""
                _text_response(self, text, "text/plain; charset=utf-8")
            elif parsed.path == "/api/plugins/store":
                _json_response(self, {"ok": True, "data": _store_registry()})
            elif parsed.path == "/api/knowledge/documents":
                _json_response(self, {"ok": True, "data": _knowledge_base.list_documents()})
            elif parsed.path == "/api/knowledge/status":
                _json_response(self, {"ok": True, "data": {"documents": _knowledge_base.list_documents()}})
            elif parsed.path == "/api/knowledge/capabilities":
                _json_response(self, {"ok": True, "data": {"upload_path": "/api/knowledge/documents/upload", "version": 1}})
            elif parsed.path == "/api/chat/models":
                _json_response(self, {"ok": True, "data": _chatroom_models()})
            elif parsed.path == "/api/chat/sessions":
                _json_response(self, {"ok": True, "data": _chatroom_list_sessions()})
            elif parsed.path == "/api/chat/session":
                qs = urllib.parse.parse_qs(parsed.query)
                sid = (qs.get("id") or [""])[0]
                obj = _chatroom_load(sid)
                if obj is None:
                    _json_response(self, {"ok": False, "error": "会话不存在"}, 404)
                else:
                    _json_response(self, {"ok": True, "data": _chatroom_public(obj)})
            elif parsed.path == "/api/trace/list":
                qs = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int((qs.get("limit") or ["0"])[0] or 0)
                except (TypeError, ValueError):
                    limit = 0
                _json_response(self, {"ok": True, "data": _trace_store().list_records(limit=limit or None)})
            elif parsed.path == "/api/trace/detail":
                qs = urllib.parse.parse_qs(parsed.query)
                obj = _trace_store().get_record((qs.get("id") or [""])[0])
                if obj is None:
                    _json_response(self, {"ok": False, "error": "记录不存在或已被新记录挤出保存上限"}, 404)
                else:
                    _json_response(self, {"ok": True, "data": obj})
            elif parsed.path == "/api/trace/switch":
                _json_response(self, {"ok": True, "data": {"enabled": bool(_trace_store().enabled)}})
            else:
                _json_response(self, {"ok": False, "error": "Not Found"}, 404)
        except Exception as e:
            _json_response(self, {"ok": False, "error": str(e), "traceback": traceback.format_exc()}, 500)

    def do_POST(self):
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/send":
            body_limit = 4 * 1024 * 1024
        elif parsed.path in ("/api/knowledge/documents/upload", "/api/knowledge/upload"):
            # 16MB 原文件经 base64 后约 21.34MB，再加 JSON 字段会超过通用 20MB。
            body_limit = 24 * 1024 * 1024
        else:
            body_limit = 20 * 1024 * 1024
        data, err = self._read_body_json(max_bytes=body_limit)
        if err:
            status = 413 if err == "请求体过大" else 400
            code = "PAYLOAD_TOO_LARGE" if status == 413 else "INVALID_JSON"
            _json_response(self, {"ok": False, "code": code, "error": err if status == 413 else "JSON 解析失败: " + err}, status)
            return
        try:
            if parsed.path == "/api/config":
                save_config_bundle(data or {})
                if callable(_config_saved_callback):
                    _config_saved_callback()
                _json_response(self, {"ok": True, "message": "配置已保存并已尝试热应用。", "data": collect_config_bundle()})
            elif parsed.path == "/api/auth/set-token":
                # 首次设置 Token。设过之后此接口已被 _guard 保护，
                # 想改 Token 得先登录，或直接改 config.json。
                current = str(get_webui_config().get("access_token", "") or "").strip()
                if current:
                    raise ValueError("已设置过访问 Token。如需修改请在「WebUI」页面改，或直接编辑 config.json")
                new_token = str((data or {}).get("token", "") or "").strip()
                if len(new_token) < 8:
                    raise ValueError("Token 至少需要 8 个字符")
                if len(new_token) > 256:
                    raise ValueError("Token 过长（超过 256 字符）")
                with config_transaction(CONFIG_PATH):
                    cfg = read_json(CONFIG_PATH, {})
                    webui_cfg = cfg.setdefault("WebUI", {})
                    if not isinstance(webui_cfg, dict):
                        webui_cfg = {}
                        cfg["WebUI"] = webui_cfg
                    webui_cfg["access_token"] = new_token
                    write_json(CONFIG_PATH, cfg)
                if callable(_config_saved_callback):
                    _config_saved_callback()
                _json_response(self, {"ok": True, "message": "访问 Token 已设置，请用它登录。"})
            elif parsed.path == "/api/features":
                payload = data or {}
                feature_switches = payload.get("feature_switches", payload)
                with config_transaction(CONFIG_PATH):
                    cfg = read_json(CONFIG_PATH, {})
                    raw = cfg.get("FeatureSwitches", {})
                    merged = dict(DEFAULT_FEATURE_SWITCHES)
                    if isinstance(raw, dict):
                        for key in merged.keys():
                            if key in raw:
                                merged[key] = normalize_bool_config(raw.get(key), default=merged[key])
                    if isinstance(feature_switches, dict):
                        for key in merged.keys():
                            if key in feature_switches:
                                merged[key] = normalize_bool_config(feature_switches[key], default=merged[key])
                    cfg["FeatureSwitches"] = {"_comment": raw.get("_comment", "功能热开关") if isinstance(raw, dict) else "功能热开关", **merged}
                    write_json(CONFIG_PATH, cfg)
                if callable(_config_saved_callback):
                    _config_saved_callback()
                _json_response(self, {"ok": True, "message": "功能开关已保存并热应用。", "data": {"feature_switches": merged, "feature_meta": FEATURE_META}})
            elif parsed.path == "/api/webui/background/upload":
                payload = data or {}
                filename = str(payload.get("filename", "") or "")
                img_b64 = str(payload.get("image_b64", "") or "")
                # base64 膨胀约 4/3，先按文本长度粗筛，避免先解码再判断白吃内存
                if len(img_b64) > MAX_BACKGROUND_BYTES * 4 // 3 + 1024:
                    raise ValueError(f"图片过大（上限 {MAX_BACKGROUND_BYTES // 1048576}MB）")
                # 前端可能连 data:image/png;base64, 前缀一起发过来
                if "," in img_b64[:64]:
                    img_b64 = img_b64.split(",", 1)[1]
                try:
                    raw = base64.b64decode(img_b64, validate=False)
                except Exception:
                    raise ValueError("图片数据无效")
                rel = save_uploaded_background(filename, raw)
                with config_transaction(CONFIG_PATH):
                    cfg = read_json(CONFIG_PATH, {})
                    webui_cfg = cfg.setdefault("WebUI", {})
                    if not isinstance(webui_cfg, dict):
                        webui_cfg = {}
                        cfg["WebUI"] = webui_cfg
                    webui_cfg["background_image"] = rel
                    write_json(CONFIG_PATH, cfg)
                if callable(_config_saved_callback):
                    _config_saved_callback()
                _json_response(self, {"ok": True, "message": "背景图片已上传并生效。",
                                      "data": {"path": rel}})
            elif parsed.path == "/api/validate-config":
                # 请求能被解析为 JSON 即视为通过；这里额外校验关键字段类型。
                cfg = (data or {}).get("config_json", data or {})
                if not isinstance(cfg, dict):
                    raise ValueError("config_json 必须是对象")
                if "Connection" in cfg and not isinstance(cfg["Connection"], dict):
                    raise ValueError("Connection 必须是对象")
                if "Others" in cfg and not isinstance(cfg["Others"], dict):
                    raise ValueError("Others 必须是对象")
                _json_response(self, {"ok": True, "message": "校验通过"})
            elif parsed.path == "/api/agent/mcp":
                payload = data or {}
                action = str(payload.get("action", "") or "").strip()
                if action == "save":
                    servers = payload.get("servers")
                    if not isinstance(servers, dict):
                        raise ValueError("servers 必须是对象")
                    save_mcp_servers(servers)
                    _json_response(self, {"ok": True, "message": "MCP 配置已保存。点击「重新连接」生效。",
                                          "data": get_mcp_state()})
                elif action == "reload":
                    _json_response(self, {"ok": True, "message": reload_mcp_servers(),
                                          "data": get_mcp_state()})
                else:
                    raise ValueError("action 只能是 save 或 reload")
            elif parsed.path == "/api/ui-state":
                save_ui_state(data or {})
                _json_response(self, {"ok": True, "message": "设置已保存并已尝试热应用。", "data": collect_ui_state()})
            elif parsed.path == "/api/update/check":
                _json_response(self, {"ok": True, "message": "已检查更新", "data": fetch_update_info(force=True)})
            elif parsed.path == "/api/providers/detect-models":
                payload = data or {}
                base_url = str(payload.get("base_url", "") or "").strip().rstrip("/")
                keys = _normalize_provider_keys(payload.get("keys", []))
                if not base_url:
                    raise ValueError("base_url 不能为空")
                if not keys:
                    raise ValueError("至少需要一个 key 才能检测模型")
                req = urllib.request.Request(
                    base_url + "/models",
                    headers={"Authorization": f"Bearer {keys[0]}", "User-Agent": "XcBot-WebUI/1.0"},
                    method="GET",
                )
                try:
                    with _make_opener().open(req, timeout=20) as resp:
                        obj = json.loads(resp.read().decode("utf-8", errors="replace"))
                    models = []
                    for item in obj.get("data", []) if isinstance(obj, dict) else []:
                        if isinstance(item, dict) and str(item.get("id", "") or "").strip():
                            models.append(str(item.get("id")).strip())
                    _json_response(self, {"ok": True, "message": f"检测到 {len(models)} 个模型", "data": {"models": models, "error": ""}})
                except Exception as e:
                    _json_response(self, {"ok": True, "message": "检测失败", "data": {"models": [], "error": str(e)}})
            elif parsed.path in ("/api/knowledge/documents/upload", "/api/knowledge/upload"):
                payload = data or {}
                filename = str(payload.get("filename", "") or "")
                content_b64 = str(payload.get("content_b64", "") or "")
                if len(content_b64) > 22 * 1024 * 1024:
                    raise ValueError("知识库文件过大（上限 16MB）")
                try:
                    raw = base64.b64decode(content_b64.split(",", 1)[-1], validate=False)
                except Exception as e:
                    raise ValueError("知识库文件数据无效") from e
                cfg = read_json(CONFIG_PATH, {})
                kb_cfg = cfg.get("KnowledgeBase", {}) if isinstance(cfg.get("KnowledgeBase", {}), dict) else {}
                providers = (cfg.get("Others", {}) or {}).get("llm_providers", [])
                result = _knowledge_base.add_document(filename, raw, kb_cfg, providers if isinstance(providers, list) else [])
                status = str(result.get("status", "") or "")
                if status == "indexing":
                    msg = "文档正在索引中，请稍后刷新查看结果"
                elif status == "failed":
                    msg = f"文档索引失败：{str(result.get('error', '') or '未知错误')}"
                elif status == "ready" and result.get("error"):
                    msg = f"文档已建立索引（向量模式降级为 FTS：{result['error']}）"
                else:
                    msg = "知识库文档已上传并完成索引"
                _json_response(self, {"ok": True, "data": result, "message": msg})
            elif parsed.path == "/api/knowledge/documents/delete":
                payload = data or {}
                if not _knowledge_base.delete_document(str(payload.get("document_id", "") or "")):
                    raise ValueError("知识库文档不存在")
                _json_response(self, {"ok": True, "message": "知识库文档已删除"})
            elif parsed.path == "/api/update/install":
                install_latest_update()
                _json_response(self, {"ok": True, "message": "已开始安装更新", "data": {"install": dict(_update_install_status), "update": fetch_update_info()}})
            elif parsed.path == "/api/plugins/install":
                payload = data or {}
                name = str(payload.get("name", "") or "").strip()
                path = str(payload.get("path", "") or "").strip()
                msg = _store_install(name, path)
                _json_response(self, {"ok": True, "message": msg})
            elif parsed.path == "/api/plugins/upload":
                payload = data or {}
                name = str(payload.get("name", "") or "").strip().removesuffix(".zip")
                zip_b64 = str(payload.get("zip_b64", "") or "")
                if not name or "/" in name or "\\" in name or ".." in name:
                    raise ValueError("无效的插件名")
                # 约 12MB 原始 zip 上限（base64 膨胀后约 16MB 文本）
                if len(zip_b64) > 16 * 1024 * 1024:
                    raise ValueError("插件包过大（上限约 12MB）")
                try:
                    raw = base64.b64decode(zip_b64, validate=False)
                except Exception:
                    raise ValueError("插件 zip 数据无效")
                if len(raw) > 12 * 1024 * 1024:
                    raise ValueError("插件包过大（上限约 12MB）")
                dest = PLUGIN_DIR / name
                PLUGIN_DIR.mkdir(exist_ok=True)
                if dest.exists():
                    shutil.rmtree(dest)
                dest.mkdir(parents=True, exist_ok=True)
                written = 0
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    _check_zip_safety(zf)
                    for m in zf.namelist():
                        if m.endswith("/"):
                            continue
                        parts = m.replace("\\", "/").split("/", 1)
                        rel = parts[1] if len(parts) > 1 else parts[0]
                        if _safe_write_zip_member(dest, rel, zf.read(m)):
                            written += 1
                if written <= 0:
                    shutil.rmtree(dest, ignore_errors=True)
                    raise ValueError("插件解压失败：无有效文件或路径不合法")
                try:
                    import __main__ as main_mod  # type: ignore
                    reload_fn = getattr(main_mod, "load_plugins", None)
                    if callable(reload_fn):
                        main_mod.plugins = reload_fn()
                except Exception:
                    pass
                _json_response(self, {"ok": True, "message": f"插件 {name} 已安装，如未自动重载请发送 /重载插件"})
            elif parsed.path == "/api/plugins/uninstall":
                payload = data or {}
                name = str(payload.get("name", "") or "").strip()
                if not name or "/" in name or ".." in name:
                    raise ValueError("无效的插件名")
                dest = PLUGIN_DIR / name
                if not dest.exists():
                    raise RuntimeError(f"插件 {name} 未安装")
                shutil.rmtree(dest)
                try:
                    import __main__ as main_mod  # type: ignore
                    reload_fn = getattr(main_mod, "load_plugins", None)
                    if callable(reload_fn):
                        main_mod.plugins = reload_fn()
                except Exception:
                    pass
                _json_response(self, {"ok": True, "message": f"插件 {name} 已卸载，如未自动重载请发送 /重载插件"})
            elif parsed.path == "/api/plugins/reload":
                payload = data or {}
                name = str(payload.get("name", "") or "").strip()
                try:
                    import __main__ as main_mod  # type: ignore
                    reload_fn = getattr(main_mod, "load_plugins", None)
                    if callable(reload_fn):
                        main_mod.plugins = reload_fn()
                        _json_response(self, {"ok": True, "message": f"插件已重载"})
                    else:
                        _json_response(self, {"ok": True, "message": "请手动发送 /重载插件"})
                except Exception as e:
                    _json_response(self, {"ok": True, "message": f"重载失败: {e}，请手动发送 /重载插件"})
            elif parsed.path == "/api/chat/new":
                payload = data or {}
                obj = _chatroom_new_session(str(payload.get("model", "") or ""), str(payload.get("title", "") or ""))
                _json_response(self, {"ok": True, "data": _chatroom_public(obj)})
            elif parsed.path == "/api/chat/send-stream":
                payload = data or {}
                sid = str(payload.get("id", "") or "")
                model = str(payload.get("model", "") or "")
                text = str(payload.get("text", "") or "")
                attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
                stream_reply = bool(payload.get("stream", True))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                def _sse(event: str, obj: Dict[str, Any]):
                    self.wfile.write((f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8"))
                    self.wfile.flush()
                request_id = ""
                try:
                    with _chatroom_session_lock(sid):
                        obj, full_text = _chatroom_prepare_user_message(sid, model, text, attachments)
                        request_id = next((m.get("request_id") for m in reversed(obj.get("messages", [])) if m.get("role") == "user"), "")
                        hint = _chatroom_handle_command(full_text)
                        reply_parts = []
                        agent_state = None
                        if hint is not None:
                            reply_parts.append(hint)
                            _sse("delta", {"text": hint})
                        elif callable(_chatroom_agent_callback):
                            progress_messages = []

                            def _emit_progress(progress: str):
                                progress = str(progress or "").strip()
                                if progress:
                                    progress_messages.append(progress)
                                    _sse("progress", {"text": progress})

                            result = _chatroom_agent_callback({
                                "id": sid,
                                "model": obj.get("model") or model,
                                "text": full_text,
                                "attachments": attachments,
                                "agent_history": obj.get("_agent_history") or [],
                                "total_tokens": obj.get("_agent_total_tokens") or 0,
                                "total_calls": obj.get("_agent_total_calls") or 0,
                                "admin": bool(str(get_webui_config().get("access_token", "") or "").strip()),
                                "stream": stream_reply,
                                "progress_callback": _emit_progress,
                                "stream_callback": lambda chunk: _sse("delta", {"text": str(chunk or "")}),
                            })
                            if not isinstance(result, dict):
                                raise RuntimeError("聊天室 Agent 回调返回格式无效")
                            reply_parts.append(str(result.get("reply") or ""))
                            agent_state = result
                        else:
                            llm_messages = _chatroom_build_llm_messages(obj, obj.get("model") or model)
                            if stream_reply:
                                for part in _chatroom_stream_complete(obj.get("model") or model, llm_messages):
                                    reply_parts.append(part)
                                    _sse("delta", {"text": part})
                            else:
                                reply = _chatroom_complete(obj.get("model") or model, llm_messages)
                                reply_parts.append(reply)
                                _sse("delta", {"text": reply})
                        fresh = _chatroom_append_assistant(
                            obj, "".join(reply_parts), agent_state=agent_state,
                            progress_messages=_chatroom_progress_messages(agent_state),
                        )
                        _sse("done", {"session": _chatroom_public(fresh)})
                except (BrokenPipeError, ConnectionResetError):
                    _chatroom_remove_pending_user(sid, request_id)
                except Exception as e:
                    _chatroom_remove_pending_user(sid, request_id)
                    try:
                        _sse("error", {"error": str(e)})
                    except Exception:
                        pass

            elif parsed.path == "/api/chat/send":
                payload = data or {}
                result = _chatroom_send(str(payload.get("id", "") or ""), str(payload.get("model", "") or ""), str(payload.get("text", "") or ""), payload.get("attachments") if isinstance(payload.get("attachments"), list) else [])
                _json_response(self, {"ok": True, "data": result})

            elif parsed.path == "/api/chat/stop":
                payload = data or {}
                sid = str(payload.get("id", "") or "")
                stopped = bool(_chatroom_stop_callback(sid)) if callable(_chatroom_stop_callback) else False
                _json_response(self, {"ok": True, "data": {"stopped": stopped}})

            elif parsed.path == "/api/chat/model":
                payload = data or {}
                sid = str(payload.get("id", "") or "")
                model = str(payload.get("model", "") or "").strip()
                available_models = {str(item.get("model", "") or "") for item in _chatroom_models()}
                if model not in available_models:
                    _json_response(self, {"ok": False, "error": "所选模型不可用，请刷新模型列表"}, 400)
                else:
                    with _chatroom_session_lock(sid):
                        obj = _chatroom_load(sid)
                        if obj is None:
                            _json_response(self, {"ok": False, "error": "会话不存在"}, 404)
                        else:
                            obj["model"] = model
                            _chatroom_save(obj)
                            _json_response(self, {"ok": True, "data": _chatroom_public(obj)})

            elif parsed.path == "/api/chat/rename":
                payload = data or {}
                sid = str(payload.get("id", "") or "")
                with _chatroom_session_lock(sid):
                    obj = _chatroom_load(sid)
                    if obj is None:
                        _json_response(self, {"ok": False, "error": "会话不存在"}, 404)
                    else:
                        obj["title"] = (str(payload.get("title", "") or "").strip()[:60]) or obj.get("title", "新会话")
                        _chatroom_save(obj)
                        _json_response(self, {"ok": True, "data": _chatroom_public(obj)})
            elif parsed.path == "/api/chat/delete":
                payload = data or {}
                ok = _chatroom_delete(str(payload.get("id", "") or ""))
                _json_response(self, {"ok": True, "data": {"deleted": ok}})
            elif parsed.path == "/api/trace/switch":
                payload = data if isinstance(data, dict) else {}
                enabled = normalize_bool_config(payload.get("enabled"), default=False)
                _trace_store().set_enabled(enabled)
                _json_response(self, {
                    "ok": True,
                    "message": "追踪记录已" + ("开启" if enabled else "关闭"),
                    "data": {"enabled": enabled},
                })
            elif parsed.path == "/api/trace/clear":
                _trace_store().clear()
                _json_response(self, {"ok": True, "message": "追踪记录已清空", "data": {"count": 0}})
            elif parsed.path == "/api/send":
                if not is_feature_enabled_now("http_send_api", True):
                    _json_response(self, {
                        "ok": False,
                        "code": "FEATURE_DISABLED",
                        "error": "HTTP 消息发送接口已禁用，请在 WebUI 功能配置中开启",
                    }, 403)
                elif not str(get_webui_config().get("access_token", "") or "").strip():
                    _json_response(self, {
                        "ok": False,
                        "code": "TOKEN_NOT_CONFIGURED",
                        "error": "发送接口要求先配置 WebUI.access_token",
                    }, 503)
                elif not isinstance(data, dict):
                    _json_response(self, {
                        "ok": False,
                        "code": "INVALID_PAYLOAD",
                        "error": "请求体必须是 JSON 对象",
                    }, 400)
                elif not callable(_qq_send_callback):
                    _json_response(self, {
                        "ok": False,
                        "code": "SEND_NOT_READY",
                        "error": "QQ 发送功能尚未初始化",
                    }, 503)
                else:
                    result = _qq_send_callback(data)
                    if not isinstance(result, dict):
                        result = {"ok": False, "code": "INTERNAL_ERROR", "error": "发送回调返回格式无效", "status": 500}
                    status = int(result.pop("status", 200 if result.get("ok") else 500))
                    _json_response(self, result, status)
            elif parsed.path == "/api/debug/self-message":
                if not callable(_debug_self_message_callback):
                    _json_response(self, {
                        "ok": False,
                        "code": "SEND_NOT_READY",
                        "error": "调试自检功能尚未初始化，请确认 Bot 主进程已启动",
                    }, 503)
                else:
                    result = _debug_self_message_callback(data if isinstance(data, dict) else {})
                    if not isinstance(result, dict):
                        result = {"ok": False, "code": "INTERNAL_ERROR", "error": "调试回调返回格式无效", "status": 500}
                    status = int(result.pop("status", 200 if result.get("ok") else 500))
                    _json_response(self, result, status)
            else:
                _json_response(self, {"ok": False, "error": "Not Found"}, 404)
        except Exception as e:
            _json_response(self, {"ok": False, "error": str(e), "traceback": traceback.format_exc()}, 500)


def start_webui(host: Optional[str] = None, port: Optional[int] = None, on_config_saved=None) -> Optional[ThreadingHTTPServer]:
    """启动 WebUI 后台线程。重复调用不会启动多个实例。"""
    global _server, _server_thread, _config_saved_callback
    cfg = get_webui_config()
    if not cfg.get("enabled", True):
        print("WebUI 已禁用，如需启用请修改 config.json -> WebUI.enabled")
        return None
    if _server is not None:
        return _server

    _config_saved_callback = on_config_saved
    cleanup_legacy_config_files()
    install_log_capture()
    host = host or cfg["host"]
    port = int(port or cfg["port"])
    token = cfg.get("access_token", "")

    _server = ThreadingHTTPServer((host, port), WebUIHandler)
    _server_thread = threading.Thread(target=_server.serve_forever, name="XcBot-WebUI", daemon=True)
    _server_thread.start()
    url = f"http://{host}:{port}/" + (f"?token={urllib.parse.quote(token)}" if token else "")
    print(f"🌐 WebUI 已启动: {url}")
    if not str(token).strip():
        print("⚠️ 未设置 WebUI 访问 Token，打开页面后会引导你设置一个。")
        if not _is_loopback_host(host):
            print(f"   注意：当前监听 {host}（不只本机可达），设置前任何人都能读取 LLM API Key。")
    print("✅ WebUI 保存配置后将尝试热应用开关与大部分运行参数。")
    return _server


def stop_webui():
    global _server, _server_thread
    server = _server
    thread = _server_thread
    _server = None
    _server_thread = None

    if server:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    if thread and thread.is_alive() and thread is not threading.current_thread():
        try:
            thread.join(timeout=2)
        except Exception:
            pass


atexit.register(stop_webui)


LOGIN_HTML = r'''<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>XcBot WebUI 登录</title><link rel="icon" href="/assets/icon.jpg">
  <style>

    :root{--bg0:#06151b;--bg1:#0b2b26;--bg2:#12384a;--bg3:#071017;--text:#f2fbff;--muted:rgba(224,242,254,.68);--line:rgba(255,255,255,.14);--line2:rgba(255,255,255,.08);--glass:rgba(255,255,255,.105);--glass2:rgba(255,255,255,.065);--accent:#38d5ff;--accent2:#7cf7c8;--accent3:#a78bfa;--bad:#fb7185;--shadow:0 24px 90px rgba(0,0,0,.42)}
    html[data-theme="light"]{--bg0:#f4f8fb;--bg1:#eef7f3;--bg2:#edf6ff;--bg3:#f8fbff;--text:#142334;--muted:rgba(44,62,80,.68);--line:rgba(148,163,184,.24);--line2:rgba(148,163,184,.16);--glass:rgba(255,255,255,.78);--glass2:rgba(255,255,255,.58);--accent:#3b82f6;--accent2:#34d399;--accent3:#8b5cf6;--bad:#e11d48;--shadow:0 24px 72px rgba(148,163,184,.18)}
    *{box-sizing:border-box}html{min-height:100%;background:var(--bg0)}body{margin:0;min-height:100vh;color:var(--text);font-family:Segoe UI,Microsoft YaHei,Arial,sans-serif;display:grid;place-items:center;overflow:hidden;background:radial-gradient(circle at 18% 14%,rgba(124,247,200,.24),transparent 27%),radial-gradient(circle at 76% 18%,rgba(56,213,255,.18),transparent 28%),radial-gradient(circle at 82% 78%,rgba(167,139,250,.16),transparent 30%),linear-gradient(145deg,var(--bg0),var(--bg1) 42%,var(--bg2) 74%,var(--bg3))}body:after{content:"";position:fixed;inset:14px;pointer-events:none;border:1px solid rgba(255,255,255,.08);border-radius:30px;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}.login{position:relative;z-index:1;width:min(360px,calc(100vw - 40px));padding:24px 26px 28px;border:1px solid var(--line);border-radius:26px;background:linear-gradient(145deg,var(--glass),var(--glass2));box-shadow:var(--shadow);backdrop-filter:blur(24px) saturate(145%);overflow:hidden;transform:translateY(12vh)}.login:before{content:"";position:absolute;inset:-1px;border-radius:inherit;pointer-events:none;background:radial-gradient(circle at 20% 0%,rgba(124,247,200,.18),transparent 36%),radial-gradient(circle at 88% 8%,rgba(56,213,255,.16),transparent 36%)}.login>*{position:relative}.head{display:flex;justify-content:space-between;align-items:center;gap:12px}.logo{width:54px;height:54px;border-radius:17px;overflow:hidden;background:linear-gradient(135deg,var(--accent),var(--accent3));display:grid;place-items:center;box-shadow:0 14px 34px rgba(56,213,255,.25)}.logo img{width:100%;height:100%;object-fit:cover}.theme{width:38px;height:38px;border-radius:14px;border:1px solid var(--line);background:linear-gradient(180deg,var(--glass),var(--glass2));color:var(--text);cursor:pointer;font-size:17px;box-shadow:inset 0 1px 0 rgba(255,255,255,.10)}h1{font-size:23px;margin:16px 0 6px;font-weight:900;letter-spacing:.2px}.sub{font-size:13px;color:var(--muted);margin-bottom:22px}.field{height:54px;border:1px solid var(--line);border-radius:16px;margin:0 0 14px;display:grid;grid-template-columns:34px 1fr 30px;align-items:center;padding:0 11px;color:var(--muted);background:rgba(5,12,25,.24);box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}html[data-theme="light"] .field{background:rgba(255,255,255,.55)}.field:focus-within{border-color:rgba(56,213,255,.55);box-shadow:0 0 0 4px rgba(56,213,255,.12),inset 0 1px 0 rgba(255,255,255,.10)}.field svg{width:18px;height:18px;opacity:.72}.field input{width:100%;height:38px;align-self:center;border:0;outline:0;background:transparent;color:var(--text);font:inherit;padding:0;line-height:38px;display:block;transform:translateY(2px)}.field input::placeholder{color:var(--muted)}.eye{width:30px;height:30px;border:0;background:transparent;color:var(--muted);cursor:pointer;font-size:17px;display:grid;place-items:center;line-height:1;padding:0}.btn{width:100%;height:42px;border:0;border-radius:15px;background:linear-gradient(135deg,var(--accent),var(--accent3));color:#031018;font-weight:900;cursor:pointer;margin-top:12px;box-shadow:0 16px 36px rgba(56,213,255,.24)}.btn:disabled{opacity:.65;cursor:not-allowed}.msg{min-height:18px;margin-top:-4px;color:var(--bad);font-size:12px}.shake{animation:shake .22s linear 2}@keyframes shake{25%{transform:translateX(-4px)}75%{transform:translateX(4px)}}
  
  </style>
</head>
<body><main class="login" id="box"><div class="head"><div class="logo"><img src="/assets/icon.jpg" alt="XcBot"></div><button class="theme" id="themeBtn" type="button" onclick="toggleTheme()" title="切换主题">🌙</button></div><h1>XcBot WebUI</h1><div class="sub">请输入访问 Token</div><form onsubmit="login(event)"><label class="field"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M17 9V7A5 5 0 0 0 7 7v2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2Zm-8 0V7a3 3 0 0 1 6 0v2Z"/></svg><input id="tok" type="password" placeholder="访问 Token" autocomplete="current-password" autofocus><button class="eye" type="button" onclick="togglePwd()">◉</button></label><div class="msg" id="msg"></div><button class="btn" id="btn" type="submit">登录</button></form></main><script src="/static/login.js"></script></body></html>'''


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>XcBot WebUI</title><link rel="icon" href="/assets/icon.jpg">
  <link rel="stylesheet" href="/static/app.css">
</head>
<body><svg style="position:absolute;width:0;height:0;pointer-events:none" aria-hidden="true"><defs><filter id="xcbot-liquid-glass" x="-10%" y="-10%" width="120%" height="120%" primitiveUnits="userSpaceOnUse" color-interpolation-filters="sRGB"><feTurbulence type="fractalNoise" baseFrequency="0.018 0.014" numOctaves="4" seed="7" result="noise"/><feDisplacementMap in="SourceGraphic" in2="noise" scale="20" xChannelSelector="R" yChannelSelector="G"/></filter></defs></svg><div class="app"><aside class="sidebar"><div class="brand"><div class="logo"><img src="/assets/icon.jpg" alt="XcBot"></div><div><h1 id="brandName">XcBot</h1><p>实时 Web 管理台</p></div></div><div class="nav-title">功能列表</div><nav id="nav" class="nav"></nav><div class="nav-title">OneBot / Hyper 连接状态</div><div id="connectionStatus" class="pill">加载中...</div><div id="connectionDetail" class="desc" style="margin:10px 12px 0 12px"></div></aside><main class="main"><div class="topbar"><div class="title"><h2 id="pageTitle">加载中...</h2><p id="pageDesc">正在连接 WebUI</p></div><div class="toolbar"><span id="saveState" class="pill">未加载</span><button class="btn" onclick="gotoPage('chatroom')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px;vertical-align:-2px;margin-right:5px"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>聊天室</button><button class="btn" onclick="gotoPage('debug')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px;vertical-align:-2px;margin-right:5px"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>调试</button><button class="btn" id="themeBtn" onclick="toggleTheme()">深色</button><button class="btn primary" onclick="saveAll()">保存设置</button></div></div><section id="content" class="grid"></section></main></div><div id="toast" class="toast"></div><div id="submitModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;align-items:center;justify-content:center"><div style="max-width:420px;width:90%;padding:28px 32px;border-radius:var(--radius,26px);background:var(--bg2);border:1px solid var(--line);box-shadow:0 24px 90px rgba(0,0,0,.4)"><h3 style="margin:0 0 16px;color:var(--text)">提交插件</h3><div style="display:grid;gap:10px"><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px"><div><div class="label">插件名</div><input class="input" id="submitName" placeholder="your_plugin"></div><div><div class="label">作者</div><input class="input" id="submitAuthor" placeholder="你的名字"></div></div><div><div class="label">功能描述</div><textarea class="input" id="submitDesc" rows="3" placeholder="简单描述插件功能" style="resize:vertical"></textarea></div><p class="desc">提交后将打开 GitHub Issue 页面，把 zip 拖入评论框上传后点提交</p><div style="display:flex;gap:10px;justify-content:flex-end"><button class="btn" onclick="el('submitModal').style.display='none'">取消</button><button class="btn primary" onclick="storeSubmit()">打开 GitHub Issue</button></div></div></div></div><div id="leaveModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;align-items:center;justify-content:center"><div style="max-width:360px;width:90%;padding:28px 32px;border-radius:var(--radius,26px);background:var(--bg2);border:1px solid var(--line);box-shadow:0 24px 90px rgba(0,0,0,.4)"><h3 style="margin:0 0 8px;color:var(--text)">确认操作</h3><p style="margin:0 0 24px;color:var(--muted)">当前页面有未保存修改，离开后将丢失这些更改。是否离开？</p><div style="display:flex;gap:10px;justify-content:flex-end"><button class="btn" onclick="leaveCancel()">取消</button><button class="btn primary" onclick="leaveConfirm()">确定</button></div></div></div><div id="tokenModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:10000;align-items:center;justify-content:center"><div style="max-width:460px;width:90%;padding:28px 32px;border-radius:var(--radius,26px);background:var(--bg2);border:1px solid var(--line);box-shadow:0 24px 90px rgba(0,0,0,.45)"><h3 style="margin:0 0 10px;color:var(--text)">设置访问 Token</h3><p style="margin:0 0 6px;color:var(--muted);font-size:13px">当前未设置访问 Token，任何能打开这个地址的人都可以查看你的 LLM API Key、以机器人身份发消息。</p><p id="tokenModalHost" style="margin:0 0 18px;color:var(--bad,#e11d48);font-size:13px;display:none"></p><div style="display:grid;gap:10px"><div><div class="label">访问 Token（至少 8 位）</div><input class="input" id="newToken" type="password" placeholder="建议用随机字符串" autocomplete="new-password"></div><div><div class="label">再输入一次</div><input class="input" id="newToken2" type="password" placeholder="确认 Token" autocomplete="new-password"></div><div id="tokenModalMsg" class="desc" style="min-height:18px;color:var(--bad,#e11d48)"></div><div style="display:flex;gap:10px;justify-content:flex-end"><button class="btn primary" id="tokenModalBtn" onclick="submitNewToken()">保存并使用</button></div></div></div></div><div id="modelInputModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10001;align-items:center;justify-content:center" onclick="if(event.target===this)closeModelInput()"><div style="max-width:440px;width:90%;padding:28px 32px;border-radius:var(--radius,26px);background:var(--bg2);border:1px solid var(--line);box-shadow:0 24px 90px rgba(0,0,0,.45)"><h3 id="modelInputTitle" style="margin:0 0 18px;color:var(--text)">添加模型</h3><div class="field"><div class="label"><span id="modelInputLabel">模型名称</span></div><input id="modelInputValue" placeholder="例如 model-name" onkeydown="if(event.key==='Enter')submitModelInput();if(event.key==='Escape')closeModelInput()"><div class="desc">保存后模型将显示为“提供商/模型名”。</div></div><div style="display:flex;gap:10px;justify-content:flex-end;margin-top:18px"><button class="btn" onclick="closeModelInput()">取消</button><button class="btn primary" onclick="submitModelInput()">添加</button></div></div></div><div id="modelInputModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10001;align-items:center;justify-content:center" onclick="if(event.target===this)closeModelInput()"><div style="max-width:440px;width:90%;padding:28px 32px;border-radius:var(--radius,26px);background:var(--bg2);border:1px solid var(--line);box-shadow:0 24px 90px rgba(0,0,0,.45)"><h3 id="modelInputTitle" style="margin:0 0 18px;color:var(--text)">添加模型</h3><div class="field"><div class="label"><span id="modelInputLabel">模型名称</span></div><input id="modelInputValue" placeholder="例如 model-name" onkeydown="if(event.key==='Enter')submitModelInput();if(event.key==='Escape')closeModelInput()"><div class="desc">保存后模型将显示为“提供商/模型名”。</div></div><div style="display:flex;gap:10px;justify-content:flex-end;margin-top:18px"><button class="btn" onclick="closeModelInput()">取消</button><button class="btn primary" onclick="submitModelInput()">添加</button></div></div></div><input id="pluginUploadInput" type="file" accept=".zip" style="display:none" onchange="storeUploadFile(this)"><button id="pluginUploadBtn" onclick="el('pluginUploadInput').click()" title="上传本地插件" style="display:none;position:fixed;right:24px;bottom:24px;width:48px;height:48px;border-radius:50%;background:var(--accent,#6366f1);border:none;cursor:pointer;font-size:22px;color:#fff;box-shadow:0 2px 8px #0004;z-index:999">&#8679;</button>
<script src="/static/app.js"></script></body></html>'''

def _static_asset_version(filename: str) -> str:
    """用文件内容 hash 做缓存破坏，用户升级/改前端后无需强刷。

    仍可叠加程序版本号，便于日志辨认；真正触发浏览器重新下载的是内容变化。
    启动时各算一次，约几十 KB，开销可忽略；静态资源继续长期 immutable 缓存。
    """
    path = BASE_DIR / "static" / filename
    digest = "0"
    try:
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    except Exception:
        pass
    prog = ""
    try:
        prog = str(((read_json(CONFIG_PATH).get("Others") or {}).get("version_name")) or "").strip().replace(" ", "")
    except Exception:
        prog = ""
    return f"{prog}-{digest}" if prog else digest


# 启动时冻结静态资源内容，使带内容 hash 的 URL 始终对应同一份文件。
# 自动更新覆盖磁盘文件但旧进程尚未重启时，不会出现“新 app.js 调旧后端路由”。
for _asset_name in ("app.css", "app.js", "login.js"):
    try:
        _STATIC_ASSET_CONTENT[_asset_name] = (BASE_DIR / "static" / _asset_name).read_text(encoding="utf-8")
    except Exception:
        pass

# 启动时冻结静态资源内容，使带内容 hash 的 URL 始终对应同一份文件。
# 自动更新覆盖磁盘文件但旧进程尚未重启时，不会出现“新 app.js 调旧后端路由”。
for _asset_name in ("app.css", "app.js", "login.js"):
    try:
        _STATIC_ASSET_CONTENT[_asset_name] = (BASE_DIR / "static" / _asset_name).read_text(encoding="utf-8")
    except Exception:
        pass

# 注入到静态资源 URL：内容变了 URL 就变，浏览器自动拉新文件，普通刷新即可
INDEX_HTML = INDEX_HTML.replace('/static/app.css"', f'/static/app.css?v={_static_asset_version("app.css")}"') \
                       .replace('/static/app.js"', f'/static/app.js?v={_static_asset_version("app.js")}"')
LOGIN_HTML = LOGIN_HTML.replace('/static/login.js"', f'/static/login.js?v={_static_asset_version("login.js")}"')


if __name__ == "__main__":
    if "--standalone" not in sys.argv:
        print("⚠️ webui.py 不再默认独立运行。请通过 main.py 启动，这样主程序与 WebUI 会同时开启、同时关闭。")
        print("如确实只想单独调试 WebUI，请手动使用：python webui.py --standalone")
        sys.exit(0)

    start_webui()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_webui()
