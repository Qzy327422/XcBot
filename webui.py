# -*- coding: utf-8 -*-
"""XcBot lightweight WebUI.

只使用 Python 标准库，避免给机器人增加额外依赖。提供：
- config.json / 插件配置的读取与保存
- 运行状态、启动参数、环境信息
- stdout/stderr 实时日志缓冲与最近日志文件读取
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import atexit
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import urllib.error
import uuid
import zipfile
import re
import ctypes
import gc
from collections import Counter, defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "data" / "webui"
LOG_FILE = LOG_DIR / "runtime.log"
BOT_ICON_PATH = BASE_DIR / "assets" / "icon.jpg"
# 用于在线检查/拉取更新的 GitHub 仓库（owner/repo），可在 config.json 的 Others.github_repo 中覆盖。
GITHUB_REPO = os.environ.get("XCBOT_GITHUB_REPO", "Qzy327422/XcBot")
LEGACY_CONFIG_PATHS = [
    BASE_DIR / "Manage_User.ini",
    BASE_DIR / "Super_User.ini",
    BASE_DIR / "blacklist.sr",
    BASE_DIR / "plugins" / "split_reply_quote.json",
]

_server: Optional[ThreadingHTTPServer] = None
_server_thread: Optional[threading.Thread] = None
_started_at = time.time()
_log_buffer = deque(maxlen=2000)
_log_lock = threading.RLock()
_capture_installed = False
_capture_stdout = None
_capture_stderr = None
_config_saved_callback = None
_pre_restart_callback = None  # 由 main.py 注册：自动更新重启前释放进程锁、保存状态等
_webui_reconfigure_lock = threading.RLock()
_update_cache_lock = threading.RLock()
_update_cache = {"timestamp": 0.0, "data": None}
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
_resource_lock = threading.Lock()
_resource_sample = {
    "wall": time.time(),
    "cpu": time.process_time(),
    "percent": 0.0,
}

def _format_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分钟"
    if minutes:
        return f"{minutes}分钟 {secs}秒"
    return f"{secs}秒"


def _get_memory_usage_mb(proc: Any = None) -> float:
    try:
        import psutil  # type: ignore
        if proc is None:
            proc = psutil.Process(os.getpid())
        return round(proc.memory_info().rss / 1024 / 1024, 1)
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_uint32),
                    ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            # 显式声明返回值类型，避免在 64 位系统上 HANDLE 被截断
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                ctypes.c_uint32,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb,
            ):
                return round(counters.WorkingSetSize / 1024 / 1024, 1)
        else:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
            return round(usage / divisor, 1)
    except Exception:
        pass
    return 0.0


_psutil_proc = None

def _get_resource_usage() -> Dict[str, Any]:
    global _psutil_proc
    # 优先用 psutil 直接拿到“跨进程墙钟”的 CPU 占用率（含所有线程）
    try:
        import psutil  # type: ignore
        if _psutil_proc is None:
            _psutil_proc = psutil.Process(os.getpid())
            _psutil_proc.cpu_percent(interval=None)  # discard first value
        percent = round(_psutil_proc.cpu_percent(interval=None), 1)
        cpu_count = psutil.cpu_count(logical=True) or 1
        system_percent = round(psutil.cpu_percent(interval=None), 1)
        normalized_percent = round(percent / max(cpu_count, 1), 1)
        info = _psutil_proc.memory_info()
        usage = {
            "cpu_percent": normalized_percent,
            "cpu_percent_normalized": normalized_percent,
            "cpu_percent_process": percent,
            "cpu_percent_system": system_percent,
            "cpu_count": cpu_count,
            "memory_mb": round(info.rss / 1024 / 1024, 1),
        }
        try:
            usage.update({
                "rss_mb": round(info.rss / 1024 / 1024, 1),
                "vms_mb": round(info.vms / 1024 / 1024, 1),
                "threads": _psutil_proc.num_threads(),
                "open_files": len(_psutil_proc.open_files()),
                "connections": len(_psutil_proc.net_connections(kind='inet')),
            })
        except Exception:
            pass
        return usage
    except Exception:
        pass
    # 回落到 time.process_time（仅主线程 CPU 时间，可能偏低）
    now = time.time()
    cpu_now = time.process_time()
    with _resource_lock:
        prev_wall = float(_resource_sample.get("wall") or now)
        prev_cpu = float(_resource_sample.get("cpu") or cpu_now)
        elapsed = max(now - prev_wall, 0.001)
        cpu_delta = max(cpu_now - prev_cpu, 0.0)
        percent = round(min(100.0, (cpu_delta / elapsed) * 100.0), 1)
        cpu_count = os.cpu_count() or 1
        normalized_percent = round(percent / max(cpu_count, 1), 1)
        _resource_sample.update({"wall": now, "cpu": cpu_now, "percent": normalized_percent})
    return {"cpu_percent": normalized_percent, "cpu_percent_normalized": normalized_percent, "cpu_percent_process": percent, "cpu_count": cpu_count, "memory_mb": _get_memory_usage_mb()}



FEATURE_META = [
    {"key": "ai_chat", "title": "AI 对话", "desc": "AI 回复总开关", "group": "对话"},
    {"key": "private_chat", "title": "私聊响应", "desc": "允许私聊直接触发 AI", "group": "对话"},
    {"key": "group_chat", "title": "群聊响应", "desc": "允许群内 @ / 名字 / 前缀触发 AI", "group": "对话"},
    {"key": "sensitive_filter", "title": "屏蔽词过滤", "desc": "对消息、人设、日志展示等文本执行敏感词替换", "group": "对话"},
    {"key": "plugin_admin_commands", "title": "插件/模型命令", "desc": "允许使用 /插件视角、/model、/重载插件 等命令", "group": "功能配置"},
    {"key": "summary", "title": "群聊总结", "desc": "总结群聊记录与数据看板", "group": "功能配置"},
    {"key": "compression_commands", "title": "记忆压缩", "desc": "自动压缩上下文，并允许使用压缩相关命令", "group": "功能配置"},
    {"key": "emoji_plus_one", "title": "表情 +1", "desc": "单个表情自动复读", "group": "功能配置"},
    {"key": "split_reply_quote", "title": "分段首段引用", "desc": "开启后：仅多段回复默认首段引用发送者消息", "group": "功能配置"},
    {"key": "weak_blacklist", "title": "弱黑名单", "desc": "按概率拦截触发", "group": "功能配置"},
    {"key": "poke_reply", "title": "拍一拍回复", "desc": "收到拍一拍时自动回复", "group": "功能配置"},
    {"key": "plugins_external", "title": "外部插件加载", "desc": "是否继续加载 plugins 目录中的第三方插件", "group": "功能配置"},
]


DEFAULT_FEATURE_SWITCHES = {item["key"]: (False if item["key"] in {"plugins_external"} else True) for item in FEATURE_META}


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
    with _log_lock:
        with LOG_FILE.open("a", encoding="utf-8", errors="replace") as f:
            for line in lines:
                item = {"time": now, "stream": stream_name, "message": line}
                _log_buffer.append(item)
                f.write(f"[{now}] [{stream_name}] {line}\n")


def install_log_capture():
    global _capture_installed, _capture_stdout, _capture_stderr
    if _capture_installed:
        return
    _capture_stdout = TeeStream(sys.stdout, "stdout")
    _capture_stderr = TeeStream(sys.stderr, "stderr")
    sys.stdout = _capture_stdout
    sys.stderr = _capture_stderr
    _capture_installed = True


def read_json(path: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    if not path.exists():
        return default
    # 修复 #6：原实现一旦 config.json 半写入/损坏，整个 WebUI 全线 500。
    # 改为容错读取：失败时优先回退到最近的 .bak，再回退到 default。
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        try:
            backups = sorted(
                path.parent.glob(f"{path.name}.*.bak"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for bak in backups:
                try:
                    with bak.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    print(f"⚠️ read_json {path} 解析失败({e})，已从备份 {bak.name} 恢复读取。")
                    return data
                except Exception:
                    continue
        except Exception:
            pass
        print(f"⚠️ read_json {path} 解析失败且无可用备份，使用默认值。错误：{e}")
        return default


_WRITE_JSON_BAK_KEEP = 5


def _prune_old_backups(path: Path, keep: int = _WRITE_JSON_BAK_KEEP) -> None:
    """修复 #5：write_json 每次都写一个 .bak，长时间运行会堆几千个。这里只保留最近 keep 份。"""
    try:
        backups = sorted(
            path.parent.glob(f"{path.name}.*.bak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[keep:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + f".{datetime.now().strftime('%Y%m%d%H%M%S')}.bak")
    if path.exists():
        backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.write("\n")
    _prune_old_backups(path)


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


def _normalize_webui_llm_endpoints(value):
    result = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        base_url = str(raw.get("base_url", "") or "").strip()
        model = str(raw.get("model", "") or "").strip()
        keys_raw = raw.get("keys", [])
        if isinstance(keys_raw, str):
            keys = [x.strip() for x in keys_raw.splitlines() if x.strip()]
        elif isinstance(keys_raw, list):
            keys = [str(x).strip() for x in keys_raw if str(x).strip()]
        else:
            keys = []
        keys = [x for x in keys if not x.startswith("请输入") and x.lower() not in {"api_key", "your_api_key", "sk-xxxx"}]
        if not base_url or not keys:
            continue
        if not model:
            model = "deepseek-chat"
        result.append({
            "provider_id": str(raw.get("provider_id", "") or "").strip(),
            "base_url": base_url,
            "model": model,
            "display_model": str(raw.get("display_model", "") or "").strip() or model,
            "keys": keys,
            "supports_multimodal": normalize_bool_config(raw.get("supports_multimodal", False), default=False),
            "timeout_seconds": int(raw.get("timeout_seconds", 60) or 60),
        })
    return result


def _normalize_provider_keys(value):
    if isinstance(value, str):
        return [x.strip() for x in value.splitlines() if x.strip()]
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _provider_display_model(provider_id: str, model: str) -> str:
    provider_id = str(provider_id or "").strip()
    model = str(model or "").strip()
    return f"{provider_id}/{model}" if provider_id else model


def normalize_llm_providers_config(others: Dict[str, Any]) -> tuple[list[Dict[str, Any]], list[Dict[str, str]]]:
    others = others if isinstance(others, dict) else {}
    providers = others.get("llm_providers", [])
    if not isinstance(providers, list):
        providers = []
    provider_has_model = any(
        isinstance(p, dict)
        and str(p.get("base_url", "") or "").strip()
        and p.get("keys")
        and any(
            isinstance(m, dict)
            and str(m.get("name", "") or m.get("model", "") or "").strip()
            for m in (p.get("models", []) if isinstance(p.get("models", []), list) else [])
        )
        for p in providers
    )
    if not provider_has_model:
        converted = []
        for index, ep in enumerate(others.get("llm_endpoints", []) if isinstance(others.get("llm_endpoints", []), list) else [], start=1):
            if not isinstance(ep, dict):
                continue
            model = str(ep.get("model", "") or "deepseek-chat").strip() or "deepseek-chat"
            converted.append({
                "id": str(ep.get("provider_id", "") or f"provider{index}"),
                "base_url": str(ep.get("base_url", "") or ""),
                "keys": _normalize_provider_keys(ep.get("keys", [])),
                "models": [{
                    "name": model,
                    "enabled": True,
                    "supports_multimodal": normalize_bool_config(ep.get("supports_multimodal", False), False),
                    "timeout_seconds": int(ep.get("timeout_seconds", others.get("api_request_timeout_seconds", 60)) or 60),
                }],
                "detected_models": [],
            })
        providers = converted

    normalized_providers = []
    for raw in providers:
        if not isinstance(raw, dict):
            continue
        provider_id = str(raw.get("id", "") or "").strip()
        base_url = str(raw.get("base_url", "") or "").strip()
        keys = _normalize_provider_keys(raw.get("keys", []))
        raw_models = raw.get("models", []) if isinstance(raw.get("models", []), list) else []
        models = []
        for item in raw_models:
            if isinstance(item, str):
                item = {"name": item, "enabled": True}
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or item.get("model", "") or "").strip()
            if not name:
                continue
            try:
                timeout_seconds = int(float(item.get("timeout_seconds", others.get("api_request_timeout_seconds", 60)) or 60))
            except Exception:
                timeout_seconds = int(others.get("api_request_timeout_seconds", 60) or 60)
            models.append({
                "name": name,
                "enabled": normalize_bool_config(item.get("enabled", True), True),
                "supports_multimodal": normalize_bool_config(item.get("supports_multimodal", False), False),
                "timeout_seconds": max(1, timeout_seconds),
            })
        detected = raw.get("detected_models", [])
        if isinstance(detected, str):
            detected = [x.strip() for x in detected.splitlines() if x.strip()]
        elif isinstance(detected, list):
            detected = [str(x).strip() for x in detected if str(x).strip()]
        else:
            detected = []
        normalized_providers.append({
            "id": provider_id,
            "base_url": base_url,
            "keys": keys,
            "models": models,
            "detected_models": detected,
        })

    enabled_refs = []
    available = set()
    for provider in normalized_providers:
        pid = provider.get("id", "")
        for model in provider.get("models", []):
            if model.get("enabled"):
                ref = (pid, model.get("name", ""))
                available.add(ref)
                enabled_refs.append(ref)

    rotation = []
    seen = set()
    raw_rotation = others.get("llm_rotation", []) if isinstance(others.get("llm_rotation", []), list) else []
    for item in raw_rotation:
        if not isinstance(item, dict):
            continue
        ref = (str(item.get("provider_id", "") or "").strip(), str(item.get("model", "") or "").strip())
        if ref in available and ref not in seen:
            rotation.append({"provider_id": ref[0], "model": ref[1]})
            seen.add(ref)
    for ref in enabled_refs:
        if ref not in seen:
            rotation.append({"provider_id": ref[0], "model": ref[1]})
            seen.add(ref)
    return normalized_providers, rotation


def build_llm_endpoints_from_providers(others: Dict[str, Any]) -> list[Dict[str, Any]]:
    providers, rotation = normalize_llm_providers_config(others)
    provider_map = {p.get("id"): p for p in providers}
    result = []
    for item in rotation:
        provider = provider_map.get(item.get("provider_id"))
        if not provider:
            continue
        model_cfg = next((m for m in provider.get("models", []) if m.get("name") == item.get("model") and m.get("enabled")), None)
        if not model_cfg:
            continue
        result.append({
            "provider_id": provider.get("id", ""),
            "base_url": provider.get("base_url", ""),
            "model": model_cfg.get("name", ""),
            "display_model": _provider_display_model(provider.get("id", ""), model_cfg.get("name", "")),
            "keys": provider.get("keys", []),
            "supports_multimodal": bool(model_cfg.get("supports_multimodal", False)),
            "timeout_seconds": int(model_cfg.get("timeout_seconds", others.get("api_request_timeout_seconds", 60)) or 60),
        })
    return result


def sync_provider_config(others: Dict[str, Any]) -> None:
    providers, rotation = normalize_llm_providers_config(others)
    others["llm_providers"] = providers
    others["llm_rotation"] = rotation
    others["llm_endpoints"] = build_llm_endpoints_from_providers(others)


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
        others = cfg.get("Others", {}) if isinstance(cfg, dict) else {}
        if not isinstance(others, dict):
            others = {}
        endpoints = build_llm_endpoints_from_providers(others)
        if not endpoints:
            endpoints = _normalize_webui_llm_endpoints(others.get("llm_endpoints", []))
        key_manager.set_endpoints(endpoints)
        print(f"✅ WebUI 已直接热刷新 LLM 模型轮换: models={len(endpoints)}, keys={len(key_manager.get_all_keys())}, current={key_manager.get_current_display()}")
    except Exception as e:
        print(f"WebUI 直接热刷新 LLM 接口列表失败: {e}")


# ==================== 聊天室（WebUI 内置 AI 对话，独立沙盒） ====================
CHATROOM_DIR = BASE_DIR / "data" / "webui" / "chatroom"
_chatroom_lock = threading.RLock()
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
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _chatroom_delete(session_id: str) -> bool:
    path = _chatroom_path(session_id)
    if path.exists():
        path.unlink()
        return True
    return False


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
                with urllib.request.urlopen(req, timeout=timeout) as resp:
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
                with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def _chatroom_prepare_user_message(session_id: str, model: str, text: str, attachments: Optional[list[Dict[str, Any]]] = None) -> tuple[Dict[str, Any], str]:
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
        msg = {"role": "user", "content": text, "ts": now}
        if attachments:
            msg["attachments"] = attachments[:8]
        obj["messages"].append(msg)
        if obj.get("title", "新会话") == "新会话":
            obj["title"] = text.strip()[:30] or "新会话"
        _chatroom_save(obj)
    return obj, text


def _chatroom_append_assistant(obj: Dict[str, Any], reply: str) -> Dict[str, Any]:
    with _chatroom_lock:
        fresh = _chatroom_load(obj.get("id", "")) or obj
        fresh.setdefault("messages", []).append({"role": "assistant", "content": reply, "ts": int(time.time())})
        _chatroom_save(fresh)
        return fresh


def _chatroom_send(session_id: str, model: str, text: str, attachments: Optional[list[Dict[str, Any]]] = None) -> Dict[str, Any]:
    obj, text = _chatroom_prepare_user_message(session_id, model, text, attachments)
    hint = _chatroom_handle_command(text)
    if hint is not None:
        reply = hint
    else:
        llm_messages = _chatroom_build_llm_messages(obj, obj.get("model") or model)
        reply = _chatroom_complete(obj.get("model") or model, llm_messages)
    obj = _chatroom_append_assistant(obj, reply)
    return {"reply": reply, "session": obj}


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

    others = cfg.setdefault("Others", {})
    if not isinstance(others, dict):
        others = {}
        cfg["Others"] = others
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


def field(path: str, label: str, typ="text", desc="", default=None, options=None, category="基础") -> Dict[str, Any]:
    return {"path": path, "label": label, "type": typ, "desc": desc, "default": default, "options": options or [], "category": category}


def build_ui_schema(cfg: Dict[str, Any]) -> list[Dict[str, Any]]:
    return [
        {"key": "welcome", "title": "欢迎", "icon": "🏠", "desc": "", "fields": []},
        {"key": "stats", "title": "数据统计", "icon": "📊", "desc": "消息数、模型调用历史、模型排名与最近 1 天 Tokens Top 10", "fields": []},
        {"key": "bot", "title": "机器人", "icon": "🤖", "desc": "机器人名称、触发词和命令前缀", "fields": [
            field("Others.bot_name", "中文名", "text"),
            field("Others.bot_name_en", "英文名", "text"),
            field("Others.reminder", "命令前缀", "text", "例如 /帮助 中的 /"),
            field("Others.robot_name_triggers", "触发词", "list", "一行一个，群里提到会触发回复"),
        ]},
        {"key": "ai", "title": "AI 配置", "icon": "✨", "desc": "对话行为与分段设置", "fields": [
            field("Others.context_max_messages", "上下文最大消息数", "number"),
            field("Others.api_failure_cooldown_seconds", "失败冷却秒数", "number", "单个 API / Key 调用失败后，冷却多久再重试", 5),
            field("Others.llm_reply_failover_keywords", "回复切换关键词", "list", "一行一个。若模型回复命中其中任一关键词，则丢弃该回复并按现有失败冷却逻辑自动切换到下一个 API"),
            field("Others.llm_split.enabled", "启用 LLM 分段回复", "bool", "仅对大模型生成结果生效，不影响普通群聊回复是否引用"),
            field("Others.llm_split.mode", "LLM 分段模式", "select", "auto_prompt=大模型自主分段；regex=按正则切分模型输出", "auto_prompt", ["auto_prompt", "regex"]),
            field("Others.llm_split.prompt_suffix", "自主分段提示词", "textarea", "模式一使用。会自动追加到每次 LLM 用户消息后。建议保留 <split> 分隔符说明"),
            field("Others.llm_split.split_regex", "分段正则表达式", "textarea", "模式二使用。用于识别分段点。建议：.*?[。？！~]+|.+$"),
            field("Others.llm_split.filter_regex", "内容过滤正则表达式", "textarea", "模式二使用。对每段文本做清理，例如移除换行：\\n|\\r"),
            field("Others.llm_split.max_chars_no_split", "超过多少字不分段", "number", "最终要发送的整条内容超过[ ]字时，忽略 <split>/正则分段，改为单条发送；填 0 表示不限制", 0),
        ]},
        {"key": "providers", "title": "提供商", "icon": "⚙️", "desc": "配置提供商、检测模型并设置轮换顺序", "fields": [
            field("Others.llm_providers", "提供商", "providers"),
            field("Others.llm_rotation", "模型轮换", "rotation"),
            field("Others.api_multimodal_model", "多模态转述模型", "multimodal_model", "当主模型不支持多模态且用户发送图片时，使用这里选择的多模态模型识图转述；留空则不额外调用多模态模型"),
        ]},
        {"key": "persona", "title": "人格设定", "icon": "💗", "desc": "编辑人设", "fields": [
            field("Others.personality_presets", "人格预设", "persona_presets"),
            field("Others.active_personality_preset", "当前预设", "text"),
            field("Others.personality_prompt", "编辑人设", "textarea", "可使用 {bot_name} 与 {user_name} 占位符"),
        ]},
        {"key": "features", "title": "功能配置", "icon": "🧩", "desc": "配置功能", "fields": [
            field("Others.emoji_plus_one_cooldown_seconds", "表情 +1 冷却秒数", "number", "单个表情自动复读的防抖时间"),
            field("Others.weak_blacklist_trigger_probability", "弱黑名单回复概率", "number", "0 到 1 之间，越小越容易拦截"),
            field("Others.weak_blacklist_users", "弱黑名单用户", "list", "一行一个 QQ 号"),
            field("Others.group_random_reply_probability", "群聊概率触发概率", "number", "普通群消息命中该概率时，机器人会主动接话。支持 0~1，也兼容 0~100；填 0 表示关闭"),
            field("Others.group_random_reply_quote", "群聊概率触发时引用消息", "bool", "开启后，概率触发的回复会引用原消息；关闭则直接发送"),
            field("Others.poke_cooldown_seconds", "拍一拍冷却秒数", "number", "拍一拍自动回复的防抖时间"),
            field("Others.summary_per_day_limit", "每日总结次数", "number", "每个群每天允许总结的次数"),
            field("Others.summary_max_messages", "每次最多总结消息数", "number", "单次群聊总结最多读取多少条消息"),
            field("Others.compression_threshold", "压缩触发阈值", "number", "消息达到多少条后允许触发压缩"),
            field("Others.compression_keep_recent", "压缩保留最近消息", "number", "压缩时保留最近多少条原始消息"),
            field("Others.auto_compress_after_messages", "自动压缩消息数", "number", "消息累计到多少条时自动尝试压缩"),
        ]},
        {"key": "security", "title": "权限/名单", "icon": "🛡️", "desc": "设置管理用户和黑名单", "fields": [
            field("manage_users", "管理用户", "list", "唯一高权限入口，一行一个 "),
            field("black_list", "配置黑名单", "list", "用户号或群号，一行一个"),
        ]},
        {"key": "connection", "title": "连接", "icon": "🔌", "desc": "OneBot / Hyper 连接参数", "fields": [
            field("Connection.mode", "连接模式", "select", options=["FWS"]),
            field("Connection.host", "连接地址", "text"),
            field("Connection.port", "连接端口", "number"),
            field("Connection.listener_host", "监听地址", "text"),
            field("Connection.listener_port", "监听端口", "number"),
            field("Connection.retries", "重试次数", "number"),
            field("protocol", "协议", "select", options=["OneBot", "Satori"]),
            field("Log_level", "日志等级", "select", options=["DEBUG", "INFO", "WARNING", "ERROR"]),
        ]},
        {"key": "webui", "title": "WebUI", "icon": "🌐", "desc": "Web 管理界面自身参数", "fields": [
            field("WebUI.enabled", "启用 WebUI", "bool"),
            field("WebUI.host", "监听地址", "text"),
            field("WebUI.port", "监听端口", "number"),
            field("WebUI.access_token", "访问 Token", "password", "暴露到公网时请务必设置"),
            field("Others.github_repo", "GitHub 更新仓库", "text", "格式 owner/repo，例如 Qzy327422/XcBot；留空使用默认仓库"),
            field("Others.github_download_mirrors", "GitHub 备用更新镜像", "list", "一行一个镜像前缀。检查/下载更新时先直连 GitHub，失败后按顺序尝试这些地址；镜像会按 前缀 + GitHub原始URL 拼接"),
        ]},
        {"key": "logs", "title": "实时日志", "icon": "📜", "desc": "查看完整运行日志", "fields": []},
    ]


def get_ui_value(bundle: Dict[str, Any], path: str, default=None):
    if path == "manage_users":
        return bundle.get("manage_users", [])
    if path == "black_list":
        return bundle.get("blacklist_file", deep_get(bundle.get("config_json", {}), path, default) or [])
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
    deep_set(payload.setdefault("config_json", {}), path, value)


def collect_ui_state() -> Dict[str, Any]:
    bundle = collect_config_bundle()
    values = {}
    for section in bundle["ui_schema"]:
        for item in section.get("fields", []):
            values[item["path"]] = get_ui_value(bundle, item["path"], item.get("default"))
    return {**bundle, "form_values": values, "status": get_status(), "logs": get_recent_logs(300), "statistics": collect_statistics()}


def save_ui_state(data: Dict[str, Any]):
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
        for key in ("feature_switches", "super_users", "manage_users", "blacklist_file"):
            if key in data:
                if key in {"super_users", "manage_users"} and isinstance(values, dict) and "manage_users" in values:
                    continue
                if key == "blacklist_file" and isinstance(values, dict) and "black_list" in values:
                    continue
                payload[key] = data[key]
    payload["manage_users"] = normalize_string_list(payload.get("manage_users", []))
    payload["blacklist_file"] = normalize_string_list(payload.get("blacklist_file", []))
    save_config_bundle(payload)


def get_recent_logs(limit: int = 300) -> list[Dict[str, str]]:
    with _log_lock:
        logs = list(_log_buffer)[-limit:]
    if logs:
        return logs
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        return [{"time": "", "stream": "file", "message": line} for line in lines]
    return []


def _iter_runtime_log_lines(limit: int = 20000) -> list[str]:
    if not LOG_FILE.exists():
        return []
    try:
        return LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return []


def _parse_log_timestamp(text: str) -> Optional[int]:
    try:
        return int(datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return None


def collect_statistics() -> Dict[str, Any]:
    now = int(time.time())
    one_day_ago = now - 86400
    lines = _iter_runtime_log_lines(40000)

    total_messages = 0
    message_trend = Counter()
    message_scene = Counter()
    api_request_history: list[Dict[str, Any]] = []
    model_rank = defaultdict(lambda: {"calls": 0, "tokens": 0, "success": 0, "failure": 0})
    session_tokens_1d = defaultdict(lambda: {"tokens": 0, "calls": 0, "last_time": 0})
    token_trend_1d = Counter()
    pending_api_by_scene: dict[str, Dict[str, Any]] = {}

    api_req_re = re.compile(
        r"^\[(?P<time>[^\]]+)\] \[[^\]]+\] \[API\] (?P<scene>.+?) -> (?P<model>.+?) @(?P<host>[^\s]+) key=(?P<key>[^\s]+) msg=(?P<msg>\d+) q=(?P<preview>.*)$"
    )
    api_ok_re = re.compile(
        r"^\[(?P<time>[^\]]+)\] \[[^\]]+\] \[API\] (?P<scene>.+?) <- (?P<model>.+?) ok tokens=(?P<tokens>\d+) a=(?P<reply>.*)$"
    )
    api_fail_re = re.compile(
        r"^\[(?P<time>[^\]]+)\] \[[^\]]+\] \[API\] (?P<scene>.+?) xx (?P<model>.+?) key=(?P<key>[^\s]+) err=(?P<error>.*)$"
    )

    for line in lines:
        req_match = api_req_re.match(line)
        if req_match:
            ts = _parse_log_timestamp(req_match.group("time")) or 0
            raw_scene = req_match.group("scene").strip()
            
            scene = raw_scene
            if raw_scene.startswith("group_"):
                scene = f"群聊 {raw_scene[6:]}"
            elif raw_scene.startswith("private_"):
                scene = f"私聊 {raw_scene[8:]}"
                
            model = req_match.group("model").strip()
            host = req_match.group("host").strip()
            total_messages += 1
            
            if ts:
                message_trend[datetime.fromtimestamp(ts).strftime("%m-%d %H:00")] += 1
            if scene.startswith("私聊"):
                message_scene["私聊"] += 1
            elif scene.startswith("群聊"):
                message_scene["群聊"] += 1
            else:
                message_scene["其他"] += 1
            item = {
                "time": req_match.group("time").strip(),
                "timestamp": ts,
                "scene": scene,
                "model": model,
                "host": host,
                "message_count": int(req_match.group("msg") or 0),
                "preview": req_match.group("preview").strip(),
                "status": "pending",
                "tokens": 0,
            }
            api_request_history.append(item)
            pending_api_by_scene[scene] = item
            stats = model_rank[model]
            stats["calls"] += 1
            continue

        ok_match = api_ok_re.match(line)
        if ok_match:
            ts = _parse_log_timestamp(ok_match.group("time")) or 0
            raw_scene = ok_match.group("scene").strip()
            scene = raw_scene
            if raw_scene.startswith("group_"):
                scene = f"群聊 {raw_scene[6:]}"
            elif raw_scene.startswith("private_"):
                scene = f"私聊 {raw_scene[8:]}"
            model = ok_match.group("model").strip()
            tokens = int(ok_match.group("tokens") or 0)
            stats = model_rank[model]
            stats["tokens"] += tokens
            stats["success"] += 1
            if ts >= one_day_ago:
                key = scene or "unknown"
                session_tokens_1d[key]["tokens"] += tokens
                session_tokens_1d[key]["calls"] += 1
                session_tokens_1d[key]["last_time"] = max(session_tokens_1d[key]["last_time"], ts)
                token_trend_1d[datetime.fromtimestamp(ts).strftime("%m-%d %H:00")] += tokens
            if scene in pending_api_by_scene:
                pending_api_by_scene[scene]["status"] = "success"
                pending_api_by_scene[scene]["tokens"] = tokens
            continue

        fail_match = api_fail_re.match(line)
        if fail_match:
            raw_scene = fail_match.group("scene").strip()
            scene = raw_scene
            if raw_scene.startswith("group_"):
                scene = f"群聊 {raw_scene[6:]}"
            elif raw_scene.startswith("private_"):
                scene = f"私聊 {raw_scene[8:]}"
            model = fail_match.group("model").strip()
            model_rank[model]["failure"] += 1
            if scene in pending_api_by_scene:
                pending_api_by_scene[scene]["status"] = "failed"
            continue

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
    api_request_history = sorted(api_request_history, key=lambda x: x.get("timestamp", 0), reverse=True)[:30]

    total_api_calls = sum(item.get("calls", 0) for item in model_rank_list)
    total_api_tokens = sum(item.get("tokens", 0) for item in model_rank_list)

    return {
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


def _parse_version_parts(value: str) -> list[Any]:
    text = str(value or "").strip()
    if not text:
        return []
    text = re.sub(r"^[vV]", "", text)
    parts = []
    for token in re.findall(r"\d+|[A-Za-z]+", text):
        if token.isdigit():
            parts.append(int(token))
        else:
            parts.append(token.lower())
    return parts


def _compare_versions(current: str, latest: str) -> int:
    a = _parse_version_parts(current)
    b = _parse_version_parts(latest)
    max_len = max(len(a), len(b))
    for i in range(max_len):
        av = a[i] if i < len(a) else 0
        bv = b[i] if i < len(b) else 0
        if type(av) is type(bv):
            if av < bv:
                return -1
            if av > bv:
                return 1
            continue
        avs, bvs = str(av), str(bv)
        if avs < bvs:
            return -1
        if avs > bvs:
            return 1
    return 0


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


UPDATE_SKIP_NAMES = {".git", ".github", "config_backup", "data", "temps", "Tools", "__pycache__", "my_bot.lock", "update_backup"}


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
    """供 main.py 注册：自动更新完成在拉起新进程之前会被调用一次，
    用来释放进程锁、保存内存状态等。失败不会中断重启流程。
    """
    global _pre_restart_callback
    _pre_restart_callback = fn


def _restart_current_process_after_update() -> None:
    """自动更新完成后，拉起新进程并退出旧进程。

    修复点：原实现旧进程用 os._exit(0) 退出，不会触发 atexit，
    msvcrt.locking / fcntl.flock 持有的进程锁不会释放，
    新进程几乎必然抢锁失败 sys.exit(1)。改进：
      1) 启动新进程前调用 _pre_restart_callback（main.py 注入 release_lock）；
      2) 旧进程退出前先 stop_webui / flush，并把等待时间放大到 1.8s 给新进程喘息；
      3) 保留原 close_fds，但显式 stdin=DEVNULL/stdout=stderr，避免句柄继承导致的 Windows 锁残留。
    """
    # 先尝试释放进程锁等外部资源（仅旧进程一次）
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
        # 注意：不要用 DETACHED_PROCESS / DEVNULL 重定向 stdin/stdout/stderr。
        # 那样新进程会脱离当前控制台，看不到日志。锁残留的问题已经靠前面的
        # _pre_restart_callback(release_lock) 解决，这里让子进程默认继承父进程的
        # 控制台句柄即可，行为跟手动 `python main.py` 一样。
        subprocess.Popen(
            argv,
            cwd=str(BASE_DIR),
            close_fds=False,  # 继承控制台句柄，保证新进程能往同一个 cmd 窗口打印
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
                github_zip_url = zip_url or (f"https://github.com/{repo}/archive/refs/tags/{raw_tag}.zip" if repo and tag_name else "")
                if github_zip_url:
                    download_candidates.extend(_github_accelerated_urls(github_zip_url))
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
                            while True:
                                chunk = resp.read(1024 * 256)
                                if not chunk:
                                    break
                                f.write(chunk)
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
                    zf.extractall(extract_dir)

                candidates = [x for x in extract_dir.iterdir() if x.is_dir()]
                if not candidates:
                    raise RuntimeError("更新包解压失败：未找到项目目录")
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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        "resource_usage": _get_resource_usage(),
        "gc": {
            "counts": list(gc.get_count()),
            "thresholds": list(gc.get_threshold()),
            "tracked_objects": len(gc.get_objects()),
        },
        "webui": {
            "log_buffer": _safe_len(_log_buffer),
            "update_cache": bool(_update_cache.get("data")),
        },
        "runtime": {},
    }

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
            runtime["token_stats"] = {
                "total_tokens": getattr(token_stats, "total_tokens", 0),
                "sessions": _safe_len(getattr(token_stats, "session_tokens", {})),
                "users": _safe_len(getattr(token_stats, "user_tokens", {})),
                "groups": _safe_len(getattr(token_stats, "group_tokens", {})),
                "detail_sessions": _safe_len(getattr(token_stats, "detailed_stats", {})),
            }

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
            for c in list(groups.values()) + list(privates.values()):
                client_pool += _safe_len(getattr(c, "_client_pool", {}))
            ctx_info = {
                "group_contexts": _safe_len(groups),
                "private_contexts": _safe_len(privates),
                "loaded_messages": sum(_ctx_msgs(c) for c in list(groups.values()) + list(privates.values())),
                "client_pool": client_pool,
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
                    api = {
                        "total": _safe_len(status_list),
                        "active": active,
                        "cooldown": cooldown,
                        "disabled": disabled,
                        "multimodal": multimodal,
                        "fail_total": fails,
                        "current": km.get_current_display() if hasattr(km, "get_current_display") else "",
                        "default": km.get_default_display() if hasattr(km, "get_default_display") else "",
                        "switch_logs": _safe_len(getattr(km, "switch_logs", [])),
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
            "root": _safe_len(getattr(main_mod, "ROOT_User", [])),
            "super": _safe_len(getattr(main_mod, "Super_User", [])),
            "manage": _safe_len(getattr(main_mod, "Manage_User", [])),
        }
        get_bl = getattr(main_mod, "get_all_blacklist", None)
        if callable(get_bl):
            try:
                perm["blacklist"] = _safe_len(get_bl())
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
            "cooldowns": _safe_len(getattr(main_mod, "cooldowns", {})),
            "poke_cooldowns": _safe_len(getattr(main_mod, "poke_cooldowns", {})),
            "summary_groups": _safe_len(getattr(main_mod, "daily_summary_records", {})),
            "generating": bool(getattr(main_mod, "generating", False)),
            "running": bool(getattr(main_mod, "running", False)),
        }
        second_start = getattr(main_mod, "second_start", None)
        if isinstance(second_start, (int, float)):
            runtime["counters"]["uptime_seconds"] = int(time.time() - second_start)

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
        "zipball_url": zipball_url,
        "body": body,
    })

    with _update_cache_lock:
        _update_cache["timestamp"] = now
        _update_cache["data"] = dict(result)
    return result


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


def _text_response(handler: BaseHTTPRequestHandler, text: str, content_type="text/html; charset=utf-8", status: int = 200):
    body = text.encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass


def _binary_response(handler: BaseHTTPRequestHandler, body: bytes, content_type="application/octet-stream", status: int = 200):
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "public, max-age=3600")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
        pass


class WebUIHandler(BaseHTTPRequestHandler):
    server_version = "XcBotWebUI/1.0"

    def log_message(self, fmt, *args):
        try:
            message = fmt % args
        except Exception:
            message = str(fmt)

        # 忽略前端自动轮询产生的高频访问日志，避免刷屏
        if 'GET /api/ui-state HTTP/1.1' in message:
            return

        _append_log("WebUI " + message, "webui")

    def _auth_ok(self) -> bool:
        token = get_webui_config().get("access_token", "")
        if not token:
            return True
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        header_token = self.headers.get("X-WebUI-Token", "")
        return header_token == token or (qs.get("token") or [""])[0] == token

    def _read_body_json(self) -> Tuple[Optional[Any], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            return json.loads(raw), None
        except Exception as e:
            return None, str(e)

    def _guard(self) -> bool:
        if self.path.startswith("/api/") and not self._auth_ok():
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
            elif parsed.path == "/api/status":
                _json_response(self, {"ok": True, "data": get_status()})
            elif parsed.path == "/api/config":
                _json_response(self, {"ok": True, "data": collect_config_bundle()})
            elif parsed.path == "/api/logs":
                qs = urllib.parse.parse_qs(parsed.query)
                limit = int((qs.get("limit") or ["300"])[0])
                _json_response(self, {"ok": True, "data": get_recent_logs(limit)})
            elif parsed.path == "/api/features":
                bundle = collect_config_bundle()
                _json_response(self, {"ok": True, "data": {"feature_switches": bundle.get("feature_switches", {}), "feature_meta": bundle.get("feature_meta", FEATURE_META)}})
            elif parsed.path == "/api/ui-state":
                _json_response(self, {"ok": True, "data": collect_ui_state()})
            elif parsed.path == "/api/statistics":
                _json_response(self, {"ok": True, "data": collect_statistics()})
            elif parsed.path == "/api/update/check":
                _json_response(self, {"ok": True, "data": fetch_update_info(force=True)})
            elif parsed.path == "/api/raw-log":
                text = LOG_FILE.read_text(encoding="utf-8", errors="replace") if LOG_FILE.exists() else ""
                _text_response(self, text, "text/plain; charset=utf-8")
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
                    _json_response(self, {"ok": True, "data": obj})
            else:
                _json_response(self, {"ok": False, "error": "Not Found"}, 404)
        except Exception as e:
            _json_response(self, {"ok": False, "error": str(e), "traceback": traceback.format_exc()}, 500)

    def do_POST(self):
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        data, err = self._read_body_json()
        if err:
            _json_response(self, {"ok": False, "error": "JSON 解析失败: " + err}, 400)
            return
        try:
            if parsed.path == "/api/config":
                save_config_bundle(data or {})
                _json_response(self, {"ok": True, "message": "配置已保存并已尝试热应用。", "data": collect_config_bundle()})
            elif parsed.path == "/api/features":
                payload = data or {}
                feature_switches = payload.get("feature_switches", payload)
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
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        obj = json.loads(resp.read().decode("utf-8", errors="replace"))
                    models = []
                    for item in obj.get("data", []) if isinstance(obj, dict) else []:
                        if isinstance(item, dict) and str(item.get("id", "") or "").strip():
                            models.append(str(item.get("id")).strip())
                    _json_response(self, {"ok": True, "message": f"检测到 {len(models)} 个模型", "data": {"models": models, "error": ""}})
                except Exception as e:
                    _json_response(self, {"ok": True, "message": "检测失败", "data": {"models": [], "error": str(e)}})
            elif parsed.path == "/api/update/install":
                install_latest_update()
                _json_response(self, {"ok": True, "message": "已开始安装更新", "data": {"install": dict(_update_install_status), "update": fetch_update_info()}})
            elif parsed.path == "/api/chat/new":
                payload = data or {}
                obj = _chatroom_new_session(str(payload.get("model", "") or ""), str(payload.get("title", "") or ""))
                _json_response(self, {"ok": True, "data": obj})
            elif parsed.path == "/api/chat/send-stream":
                payload = data or {}
                sid = str(payload.get("id", "") or "")
                model = str(payload.get("model", "") or "")
                text = str(payload.get("text", "") or "")
                attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                def _sse(event: str, obj: Dict[str, Any]):
                    self.wfile.write((f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8"))
                    self.wfile.flush()
                try:
                    obj, full_text = _chatroom_prepare_user_message(sid, model, text, attachments)
                    hint = _chatroom_handle_command(full_text)
                    reply_parts = []
                    if hint is not None:
                        reply_parts.append(hint)
                        _sse("delta", {"text": hint})
                    else:
                        llm_messages = _chatroom_build_llm_messages(obj, obj.get("model") or model)
                        for part in _chatroom_stream_complete(obj.get("model") or model, llm_messages):
                            reply_parts.append(part)
                            _sse("delta", {"text": part})
                    fresh = _chatroom_append_assistant(obj, "".join(reply_parts))
                    _sse("done", {"session": fresh})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    try:
                        _sse("error", {"error": str(e)})
                    except Exception:
                        pass

            elif parsed.path == "/api/chat/send":
                payload = data or {}
                result = _chatroom_send(str(payload.get("id", "") or ""), str(payload.get("model", "") or ""), str(payload.get("text", "") or ""), payload.get("attachments") if isinstance(payload.get("attachments"), list) else [])
                _json_response(self, {"ok": True, "data": result})

            elif parsed.path == "/api/chat/rename":
                payload = data or {}
                sid = str(payload.get("id", "") or "")
                with _chatroom_lock:
                    obj = _chatroom_load(sid)
                    if obj is None:
                        _json_response(self, {"ok": False, "error": "会话不存在"}, 404)
                    else:
                        obj["title"] = (str(payload.get("title", "") or "").strip()[:60]) or obj.get("title", "新会话")
                        _chatroom_save(obj)
                        _json_response(self, {"ok": True, "data": obj})
            elif parsed.path == "/api/chat/delete":
                payload = data or {}
                ok = _chatroom_delete(str(payload.get("id", "") or ""))
                _json_response(self, {"ok": True, "data": {"deleted": ok}})
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
    _server = ThreadingHTTPServer((host, port), WebUIHandler)
    _server_thread = threading.Thread(target=_server.serve_forever, name="XcBot-WebUI", daemon=True)
    _server_thread.start()
    token = cfg.get("access_token", "")
    url = f"http://{host}:{port}/" + (f"?token={urllib.parse.quote(token)}" if token else "")
    print(f"🌐 WebUI 已启动: {url}")
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
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>XcBot WebUI 登录</title><link rel="icon" href="/assets/icon.jpg">
  <style>
    :root{--bg0:#06151b;--bg1:#0b2b26;--bg2:#12384a;--bg3:#071017;--text:#f2fbff;--muted:rgba(224,242,254,.68);--line:rgba(255,255,255,.14);--line2:rgba(255,255,255,.08);--glass:rgba(255,255,255,.105);--glass2:rgba(255,255,255,.065);--accent:#38d5ff;--accent2:#7cf7c8;--accent3:#a78bfa;--bad:#fb7185;--shadow:0 24px 90px rgba(0,0,0,.42)}
    html[data-theme="light"]{--bg0:#f4f8fb;--bg1:#eef7f3;--bg2:#edf6ff;--bg3:#f8fbff;--text:#142334;--muted:rgba(44,62,80,.68);--line:rgba(148,163,184,.24);--line2:rgba(148,163,184,.16);--glass:rgba(255,255,255,.78);--glass2:rgba(255,255,255,.58);--accent:#3b82f6;--accent2:#34d399;--accent3:#8b5cf6;--bad:#e11d48;--shadow:0 24px 72px rgba(148,163,184,.18)}
    *{box-sizing:border-box}html{min-height:100%;background:var(--bg0)}body{margin:0;min-height:100vh;color:var(--text);font-family:Segoe UI,Microsoft YaHei,Arial,sans-serif;display:grid;place-items:center;overflow:hidden;background:radial-gradient(circle at 18% 14%,rgba(124,247,200,.24),transparent 27%),radial-gradient(circle at 76% 18%,rgba(56,213,255,.18),transparent 28%),radial-gradient(circle at 82% 78%,rgba(167,139,250,.16),transparent 30%),linear-gradient(145deg,var(--bg0),var(--bg1) 42%,var(--bg2) 74%,var(--bg3))}body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.24;background-image:linear-gradient(rgba(255,255,255,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.055) 1px,transparent 1px);background-size:38px 38px}body:after{content:"";position:fixed;inset:14px;pointer-events:none;border:1px solid rgba(255,255,255,.08);border-radius:30px;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}.login{position:relative;z-index:1;width:min(360px,calc(100vw - 40px));padding:24px 26px 28px;border:1px solid var(--line);border-radius:26px;background:linear-gradient(145deg,var(--glass),var(--glass2));box-shadow:var(--shadow);backdrop-filter:blur(24px) saturate(145%);overflow:hidden;transform:translateY(12vh)}.login:before{content:"";position:absolute;inset:-1px;border-radius:inherit;pointer-events:none;background:radial-gradient(circle at 20% 0%,rgba(124,247,200,.18),transparent 36%),radial-gradient(circle at 88% 8%,rgba(56,213,255,.16),transparent 36%)}.login>*{position:relative}.head{display:flex;justify-content:space-between;align-items:center;gap:12px}.logo{width:54px;height:54px;border-radius:17px;overflow:hidden;background:linear-gradient(135deg,var(--accent),var(--accent3));display:grid;place-items:center;box-shadow:0 14px 34px rgba(56,213,255,.25)}.logo img{width:100%;height:100%;object-fit:cover}.theme{width:38px;height:38px;border-radius:14px;border:1px solid var(--line);background:linear-gradient(180deg,var(--glass),var(--glass2));color:var(--text);cursor:pointer;font-size:17px;box-shadow:inset 0 1px 0 rgba(255,255,255,.10)}h1{font-size:23px;margin:16px 0 6px;font-weight:900;letter-spacing:.2px}.sub{font-size:13px;color:var(--muted);margin-bottom:22px}.field{height:54px;border:1px solid var(--line);border-radius:16px;margin:0 0 14px;display:grid;grid-template-columns:34px 1fr 30px;align-items:center;padding:0 11px;color:var(--muted);background:rgba(5,12,25,.24);box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}html[data-theme="light"] .field{background:rgba(255,255,255,.55)}.field:focus-within{border-color:rgba(56,213,255,.55);box-shadow:0 0 0 4px rgba(56,213,255,.12),inset 0 1px 0 rgba(255,255,255,.10)}.field svg{width:18px;height:18px;opacity:.72}.field input{width:100%;height:38px;align-self:center;border:0;outline:0;background:transparent;color:var(--text);font:inherit;padding:0;line-height:38px;display:block;transform:translateY(2px)}.field input::placeholder{color:var(--muted)}.eye{width:30px;height:30px;border:0;background:transparent;color:var(--muted);cursor:pointer;font-size:17px;display:grid;place-items:center;line-height:1;padding:0}.btn{width:100%;height:42px;border:0;border-radius:15px;background:linear-gradient(135deg,var(--accent),var(--accent3));color:#031018;font-weight:900;cursor:pointer;margin-top:12px;box-shadow:0 16px 36px rgba(56,213,255,.24)}.btn:disabled{opacity:.65;cursor:not-allowed}.msg{min-height:18px;margin-top:-4px;color:var(--bad);font-size:12px}.shake{animation:shake .22s linear 2}@keyframes shake{25%{transform:translateX(-4px)}75%{transform:translateX(4px)}}
  </style>
</head>
<body><main class="login" id="box"><div class="head"><div class="logo"><img src="/assets/icon.jpg" alt="XcBot"></div><button class="theme" id="themeBtn" type="button" onclick="toggleTheme()" title="切换主题">🌙</button></div><h1>XcBot WebUI</h1><div class="sub">请输入访问 Token</div><form onsubmit="login(event)"><label class="field"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M17 9V7A5 5 0 0 0 7 7v2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2Zm-8 0V7a3 3 0 0 1 6 0v2Z"/></svg><input id="tok" type="password" placeholder="访问 Token" autocomplete="current-password" autofocus><button class="eye" type="button" onclick="togglePwd()">◉</button></label><div class="msg" id="msg"></div><button class="btn" id="btn" type="submit">登录</button></form></main><script>
const el=id=>document.getElementById(id);function setTheme(t){document.documentElement.dataset.theme=t;localStorage.webuiTheme=t;const b=el('themeBtn');if(b)b.textContent=t==='light'?'☀️':'🌙'}function toggleTheme(){setTheme((document.documentElement.dataset.theme||'dark')==='dark'?'light':'dark')}function togglePwd(){const t=el('tok');t.type=t.type==='password'?'text':'password'}async function login(e){e.preventDefault();const t=el('tok').value.trim(),m=el('msg'),b=el('btn'),box=el('box');if(!t){m.textContent='请输入访问 Token';return}b.disabled=true;b.textContent='验证中...';try{const r=await fetch('/api/ui-state',{headers:{'X-WebUI-Token':t},cache:'no-store'});const j=await r.json().catch(()=>({ok:false,error:'验证失败'}));if(!r.ok||!j.ok)throw new Error(j.error||'Token 不正确');localStorage.webuiToken=t;location.href='/'}catch(err){m.textContent='Token 不正确或已失效';box.classList.remove('shake');void box.offsetWidth;box.classList.add('shake')}finally{b.disabled=false;b.textContent='登录'}}setTheme(localStorage.webuiTheme||'dark');
</script></body></html>'''


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>XcBot WebUI</title><link rel="icon" href="/assets/icon.jpg">
  <style>
    :root{--bg0:#06151b;--bg1:#0b2b26;--bg2:#12384a;--bg3:#071017;--glass:rgba(255,255,255,.105);--glass2:rgba(255,255,255,.072);--glass3:rgba(255,255,255,.045);--text:#f2fbff;--muted:rgba(224,242,254,.68);--muted2:rgba(224,242,254,.46);--line:rgba(255,255,255,.14);--line2:rgba(255,255,255,.08);--accent:#38d5ff;--accent2:#7cf7c8;--accent3:#a78bfa;--ok:#42e6a4;--bad:#fb7185;--shadow:0 24px 90px rgba(0,0,0,.42);--shadow2:0 12px 42px rgba(56,213,255,.14);--blur:24px;--radius:26px}
    html[data-theme="light"]{--bg0:#f4f8fb;--bg1:#eef7f3;--bg2:#edf6ff;--bg3:#f8fbff;--glass:rgba(255,255,255,.78);--glass2:rgba(255,255,255,.64);--glass3:rgba(255,255,255,.48);--text:#142334;--muted:rgba(44,62,80,.68);--muted2:rgba(44,62,80,.48);--line:rgba(148,163,184,.24);--line2:rgba(148,163,184,.16);--accent:#3b82f6;--accent2:#34d399;--accent3:#8b5cf6;--ok:#059669;--bad:#e11d48;--shadow:0 24px 72px rgba(148,163,184,.18);--shadow2:0 14px 36px rgba(59,130,246,.14)}
    *{box-sizing:border-box}html{min-height:100%;background:var(--bg0)}body{margin:0;min-height:100vh;color:var(--text);font-family:Inter,Segoe UI,Microsoft YaHei,Arial,sans-serif;overflow-x:hidden;background:radial-gradient(circle at 13% 9%,rgba(124,247,200,.24),transparent 27%),radial-gradient(circle at 72% 14%,rgba(56,213,255,.18),transparent 28%),radial-gradient(circle at 84% 78%,rgba(167,139,250,.16),transparent 30%),linear-gradient(145deg,var(--bg0),var(--bg1) 42%,var(--bg2) 74%,var(--bg3));background-attachment:fixed}body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.28;background-image:linear-gradient(rgba(255,255,255,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.055) 1px,transparent 1px);background-size:38px 38px}body:after{content:"";position:fixed;inset:14px;pointer-events:none;border:1px solid rgba(255,255,255,.08);border-radius:30px;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}button,a,input,textarea,select{font:inherit}button,a{color:inherit}.app{display:grid;grid-template-columns:286px 1fr;min-height:100vh;padding:18px;gap:18px;position:relative;z-index:1}.sidebar{position:sticky;top:18px;height:calc(100vh - 36px);padding:18px 14px;border:1px solid var(--line);border-radius:var(--radius);background:linear-gradient(180deg,rgba(255,255,255,.13),rgba(255,255,255,.055));box-shadow:var(--shadow);backdrop-filter:blur(var(--blur)) saturate(145%);-webkit-backdrop-filter:blur(var(--blur)) saturate(145%);overflow:auto}.brand{display:flex;align-items:center;gap:12px;padding:0 10px 18px}.logo{width:44px;height:44px;border-radius:17px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--accent3));font-size:22px;box-shadow:0 14px 34px rgba(56,213,255,.25);overflow:hidden}.logo img{width:100%;height:100%;object-fit:cover;display:block}.brand h1{font-size:17px;margin:0;font-weight:900;letter-spacing:.2px}.brand p{margin:3px 0 0;color:var(--muted);font-size:12px}.nav-title{margin:14px 12px 8px;color:var(--muted2);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.nav{display:flex;flex-direction:column;gap:8px}.nav button{border:1px solid transparent;background:transparent;text-align:left;border-radius:17px;padding:12px 13px;display:flex;align-items:center;gap:11px;cursor:pointer;color:var(--muted);font-weight:750;transition:.2s ease}.nav button:hover{color:var(--text);background:rgba(255,255,255,.075);border-color:var(--line2);transform:translateX(2px)}.nav button.active{color:var(--text);background:linear-gradient(135deg,rgba(56,213,255,.26),rgba(124,247,200,.11));border-color:rgba(56,213,255,.32);box-shadow:inset 3px 0 0 var(--accent),0 12px 28px rgba(56,213,255,.10)}.main{min-width:0;padding:0;border:1px solid var(--line);border-radius:var(--radius);background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.03));box-shadow:var(--shadow);backdrop-filter:blur(16px) saturate(135%);-webkit-backdrop-filter:blur(16px) saturate(135%);overflow:hidden}.topbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:14px;padding:18px 22px;background:linear-gradient(180deg,rgba(6,21,27,.72),rgba(6,21,27,.34));backdrop-filter:blur(22px) saturate(145%);border-bottom:1px solid var(--line2)}html[data-theme="light"] .topbar{background:linear-gradient(180deg,rgba(255,255,255,.70),rgba(255,255,255,.40))}.title h2{margin:0;font-size:24px;font-weight:950;letter-spacing:.2px}.title p{margin:5px 0 0;color:var(--muted);font-size:13px}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.btn{border:1px solid var(--line);background:linear-gradient(180deg,var(--glass),var(--glass2));border-radius:15px;padding:10px 14px;cursor:pointer;text-decoration:none;color:var(--text);font-weight:800;box-shadow:inset 0 1px 0 rgba(255,255,255,.10);transition:.2s ease}.btn:hover{transform:translateY(-1px);border-color:rgba(56,213,255,.36);box-shadow:var(--shadow2)}.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent3));border-color:transparent;color:#031018;box-shadow:0 16px 36px rgba(56,213,255,.24)}.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:linear-gradient(180deg,var(--glass),var(--glass3));border-radius:999px;padding:7px 11px;color:var(--muted);font-size:12px;font-weight:800}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:18px;padding:22px}.card{grid-column:span 12;position:relative;background:linear-gradient(145deg,rgba(255,255,255,.12),rgba(255,255,255,.055));border:1px solid var(--line);border-radius:var(--radius);padding:22px;box-shadow:0 20px 60px rgba(0,0,0,.18),inset 0 1px 0 rgba(255,255,255,.12);backdrop-filter:blur(var(--blur)) saturate(150%);-webkit-backdrop-filter:blur(var(--blur)) saturate(150%);overflow:hidden}.card:before{content:"";position:absolute;inset:-1px;border-radius:inherit;pointer-events:none;background:radial-gradient(circle at 18% 0%,rgba(124,247,200,.18),transparent 34%),radial-gradient(circle at 88% 8%,rgba(56,213,255,.16),transparent 35%)}.card>*{position:relative}.half{grid-column:span 6}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:16px}.section-head h3{margin:0;font-size:18px;font-weight:930}.section-head p{margin:5px 0 0;color:var(--muted);font-size:13px}.form-grid,.feature-grid,.mini-stats{display:grid;gap:15px}.form-grid{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}.feature-grid{grid-template-columns:repeat(auto-fit,minmax(255px,1fr))}.mini-stats{grid-template-columns:repeat(auto-fit,minmax(155px,1fr))}.field,.feature,.stat{border:1px solid var(--line2);background:linear-gradient(180deg,rgba(255,255,255,.075),rgba(255,255,255,.035));border-radius:21px;padding:15px;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}.feature{transition:.2s ease}.feature:hover{transform:translateY(-2px);border-color:rgba(56,213,255,.26);box-shadow:0 16px 36px rgba(0,0,0,.14)}.label{display:flex;justify-content:space-between;gap:10px;margin-bottom:9px;font-weight:850}.desc{color:var(--muted);font-size:12px;margin-top:9px;line-height:1.5}input,textarea,select{width:100%;border:1px solid var(--line);border-radius:16px;background:rgba(5,12,25,.34);color:var(--text);padding:11px 13px;outline:none;box-shadow:inset 0 1px 0 rgba(255,255,255,.08);transition:.18s ease}html[data-theme="light"] input,html[data-theme="light"] textarea,html[data-theme="light"] select{background:rgba(255,255,255,.55)}input:focus,textarea:focus,select:focus{border-color:rgba(56,213,255,.55);box-shadow:0 0 0 4px rgba(56,213,255,.12),inset 0 1px 0 rgba(255,255,255,.10)}textarea{min-height:132px;resize:vertical;font-family:Consolas,JetBrains Mono,monospace}.json-area{min-height:420px}.switch{position:relative;width:58px;height:32px;flex:0 0 auto;border-radius:999px;background:rgba(100,116,139,.35);border:1px solid var(--line);cursor:pointer;box-shadow:inset 0 1px 3px rgba(0,0,0,.25)}.switch:after{content:"";position:absolute;top:4px;left:4px;width:22px;height:22px;border-radius:50%;background:#dbeafe;transition:.22s cubic-bezier(.2,.8,.2,1);box-shadow:0 5px 14px rgba(0,0,0,.25)}.switch.on{background:linear-gradient(135deg,var(--accent),var(--accent2))}.switch.on:after{left:30px;background:#fff}.feature-foot,.kv{display:grid;gap:9px 12px}.feature-foot{grid-template-columns:1fr auto;align-items:center}.kv{grid-template-columns:150px 1fr;font-size:13px}.kv div:nth-child(odd),.feature p,.stat span{color:var(--muted)}.stat b{display:block;font-size:24px;font-weight:950;background:linear-gradient(135deg,var(--text),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}pre.log{margin:0;white-space:pre-wrap;word-break:break-word;max-height:560px;overflow:auto;font-family:Consolas,JetBrains Mono,monospace;font-size:12px;line-height:1.55;background:rgba(0,0,0,.22);border:1px solid var(--line);border-radius:20px;padding:16px}pre.log.compact{max-height:320px;padding:8px 16px;line-height:1.28}.toast{position:fixed;right:24px;bottom:24px;max-width:440px;border:1px solid var(--line);background:linear-gradient(180deg,var(--glass),var(--glass2));backdrop-filter:blur(22px);border-radius:18px;padding:13px 15px;display:none;box-shadow:var(--shadow);z-index:20}.toast.show{display:block}.ok{color:var(--ok)}.bad{color:var(--bad)}.file-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}@media(max-width:980px){.app{grid-template-columns:1fr;padding:12px}.sidebar{position:relative;top:auto;height:auto}.main{min-height:70vh}.topbar{padding:14px}.half{grid-column:span 12}.kv{grid-template-columns:1fr}.grid{padding:14px}.card{padding:16px}}
    .chatroom{grid-column:1/-1;display:grid;grid-template-columns:260px 1fr;gap:16px;height:calc(100vh - 150px);min-height:460px}.chat-side{display:flex;flex-direction:column;gap:10px;border:1px solid var(--line);border-radius:var(--radius);padding:14px;background:linear-gradient(180deg,rgba(255,255,255,.08),rgba(255,255,255,.03));overflow:hidden}.chat-side .sess-list{flex:1;overflow:auto;display:flex;flex-direction:column;gap:6px}.sess-item{border:1px solid var(--line2);border-radius:14px;padding:10px 12px;cursor:pointer;transition:.18s ease;display:flex;flex-direction:column;gap:3px}.sess-item:hover{background:rgba(255,255,255,.06)}.sess-item.active{background:linear-gradient(135deg,rgba(56,213,255,.22),rgba(124,247,200,.10));border-color:rgba(56,213,255,.32)}.sess-item .t{font-weight:800;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sess-item .m{color:var(--muted2);font-size:11px}.sess-actions{display:flex;gap:6px;margin-top:6px}.sess-actions .btn{padding:6px 8px;font-size:12px;border-radius:11px}.chat-main{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:var(--radius);background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.03));overflow:hidden}.chat-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line2)}.chat-head select{width:auto;min-width:180px;border-radius:12px;padding:8px 11px}.chat-msgs{flex:1;overflow:auto;padding:18px;display:flex;flex-direction:column;gap:14px}.msg{display:flex;gap:10px;max-width:82%}.msg.user{align-self:flex-end;flex-direction:row-reverse}.msg .bubble{padding:11px 14px;border-radius:16px;white-space:normal;word-break:break-word;line-height:1.65;font-size:14px}.msg.user .bubble{background:linear-gradient(135deg,var(--accent),var(--accent3));color:#031018;border-bottom-right-radius:5px}.msg.assistant .bubble{background:rgba(255,255,255,.08);border:1px solid var(--line2);border-bottom-left-radius:5px}.md{display:grid;gap:.72em}.md p{margin:0}.md h1,.md h2,.md h3{margin:.2em 0 .05em;line-height:1.35;font-weight:900;letter-spacing:.01em}.md h1{font-size:1.28em}.md h2{font-size:1.16em}.md h3{font-size:1.06em}.md hr{width:100%;height:1px;border:0;background:linear-gradient(90deg,transparent,var(--line),transparent);margin:.35em 0}.md ul,.md ol{margin:0;padding-left:1.45em;display:grid;gap:.24em}.md li{padding-left:.15em}.md blockquote{margin:0;padding:.65em .85em;border-left:3px solid var(--accent);background:rgba(56,213,255,.08);border-radius:10px;color:var(--muted)}.md-code{border-radius:14px;background:rgba(0,0,0,.24);border:1px solid var(--line2);overflow:hidden;margin:.15em 0}.md-code-head{height:34px;padding:0 10px;display:flex;align-items:center;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line2);color:var(--muted);font-size:12px;background:rgba(255,255,255,.035)}.md-copy{border:1px solid var(--line2);background:rgba(255,255,255,.06);color:var(--text);border-radius:9px;padding:4px 9px;font-size:12px;cursor:pointer}.md-copy:hover{border-color:rgba(56,213,255,.45);background:rgba(56,213,255,.12)}.md pre{margin:0;padding:12px 14px;background:transparent;overflow:auto;white-space:pre;line-height:1.55}.md code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.92em}.md :not(pre)>code{padding:.12em .38em;border-radius:7px;background:rgba(0,0,0,.22);border:1px solid rgba(255,255,255,.08)}.md-table-wrap{overflow:auto;border:1px solid var(--line2);border-radius:14px;background:rgba(0,0,0,.12)}.md table{width:100%;border-collapse:collapse;font-size:.95em}.md th,.md td{padding:8px 10px;border-bottom:1px solid var(--line2);text-align:left;vertical-align:top}.md th{font-weight:900;background:rgba(255,255,255,.055)}.md tr:last-child td{border-bottom:0}.md a{color:var(--accent);text-decoration:none;border-bottom:1px solid rgba(56,213,255,.35)}.msg .av{width:32px;height:32px;border-radius:10px;flex:0 0 auto;display:grid;place-items:center;font-size:16px;background:rgba(255,255,255,.08)}.chat-composer{margin:0 16px 16px;border:1px solid var(--line2);border-radius:22px;background:rgba(255,255,255,.055);padding:10px;display:grid;gap:8px}.chat-files{display:flex;gap:8px;flex-wrap:wrap}.chat-file{display:flex;align-items:center;gap:7px;max-width:220px;padding:6px 9px;border:1px solid var(--line2);border-radius:999px;background:rgba(255,255,255,.06);font-size:12px;color:var(--muted)}.chat-file button{border:0;background:transparent;color:var(--muted);cursor:pointer}.chat-input{display:grid;grid-template-columns:auto 1fr auto auto;gap:8px;align-items:end}.chat-icon{width:36px;height:36px;border-radius:50%;border:1px solid var(--line2);background:rgba(255,255,255,.05);color:var(--text);display:grid;place-items:center;line-height:1;padding:0;cursor:pointer}.chat-icon.on{border-color:rgba(56,213,255,.55);background:rgba(56,213,255,.16)}.chat-send{width:38px;height:38px;border-radius:50%;padding:0;display:grid;place-items:center;line-height:1}.chat-send.stop{background:linear-gradient(135deg,#fb7185,#f59e0b);color:#21080a}.chat-input textarea{min-height:38px;height:38px;max-height:180px;resize:none;overflow:hidden;border:0;background:transparent;padding:8px 6px;font-family:inherit}.chat-input textarea:focus{box-shadow:none}.chat-empty{margin:auto;text-align:center;color:var(--muted)}@media(max-width:980px){.chatroom{grid-template-columns:1fr;height:auto}.chat-main{min-height:60vh}}
  </style>
</head>
<body><div class="app"><aside class="sidebar"><div class="brand"><div class="logo"><img src="/assets/icon.jpg" alt="XcBot"></div><div><h1 id="brandName">XcBot</h1><p>实时 Web 管理台</p></div></div><div class="nav-title">功能列表</div><nav id="nav" class="nav"></nav><div class="nav-title">OneBot / Hyper 连接状态</div><div id="connectionStatus" class="pill">加载中...</div><div id="connectionDetail" class="desc" style="margin:10px 12px 0 12px"></div></aside><main class="main"><div class="topbar"><div class="title"><h2 id="pageTitle">加载中...</h2><p id="pageDesc">正在连接 WebUI</p></div><div class="toolbar"><span id="saveState" class="pill">未加载</span><button class="btn" onclick="gotoPage('chatroom')">💬 聊天室</button><button class="btn" onclick="gotoPage('debug')">🛠️ 调试</button><button class="btn" id="themeBtn" onclick="toggleTheme()">🌙 深色</button><button class="btn primary" onclick="saveAll()">保存设置</button></div></div><section id="content" class="grid"></section></main></div><div id="toast" class="toast"></div>
<script>
let state={bundle:null,current:'welcome',section:'',dirty:false,saving:false,lastInputAt:0,expectedReloadAfterUpdate:false,apiFailCount:0,reloadTimer:null};
const featureFieldMap={ai_chat:['Others.llm_split.enabled','Others.llm_split.mode','Others.llm_split.prompt_suffix','Others.llm_split.split_regex','Others.llm_split.filter_regex','Others.llm_split.max_chars_no_split'],group_chat:['Others.group_random_reply_probability','Others.group_random_reply_quote'],emoji_plus_one:['Others.emoji_plus_one_cooldown_seconds'],poke_reply:['Others.poke_cooldown_seconds'],split_reply_quote:[],weak_blacklist:['Others.weak_blacklist_trigger_probability','Others.weak_blacklist_users'],summary:['Others.summary_per_day_limit','Others.summary_max_messages'],compression_commands:['Others.compression_threshold','Others.compression_keep_recent','Others.auto_compress_after_messages'],plugins_external:[]};
const esc=s=>String(s??'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
const token=()=>new URLSearchParams(location.search).get('token')||localStorage.webuiToken||'';
const el=id=>document.getElementById(id);
const DRAFT_KEY='xcbotWebuiFormDraft';
function loadDraft(){try{return JSON.parse(localStorage.getItem(DRAFT_KEY)||'null')||null}catch(e){return null}}
function saveDraft(){try{if(state.bundle?.form_values)localStorage.setItem(DRAFT_KEY,JSON.stringify({fingerprint:state.bundle.config_fingerprint||'',values:state.bundle.form_values}))}catch(e){}}
function clearDraft(){try{localStorage.removeItem(DRAFT_KEY)}catch(e){}}
function applyDraft(bundle){const draft=loadDraft();if(!draft||!bundle?.form_values)return;if(!draft.fingerprint||!bundle.config_fingerprint||draft.fingerprint!==bundle.config_fingerprint){clearDraft();return}bundle.form_values=Object.assign({},bundle.form_values,draft.values||{});state.dirty=true;state.lastInputAt=Date.now();const save=el('saveState');if(save)save.textContent='有未保存草稿'}
async function api(path,opt={}){opt.headers=Object.assign({'Content-Type':'application/json','X-WebUI-Token':token()},opt.headers||{});const r=await fetch(path,opt),j=await r.json().catch(()=>({ok:false,error:'请求失败'}));if(r.status===401){localStorage.removeItem('webuiToken');location.href='/auth/login';throw new Error('请先登录')}if(!j.ok)throw new Error(j.error||'请求失败');return j.data??j}
function toast(msg,ok=true){const t=el('toast'),save=el('saveState');if(t){t.textContent=msg;t.className='toast show '+(ok?'ok':'bad');clearTimeout(t._timer);t._timer=setTimeout(()=>t.classList.remove('show'),2600)}if(save)save.textContent=msg}
const pages=()=>state.bundle?.ui_schema||[];
const routeAlias={config:'bot',normal:'bot',dashboard:'welcome',home:'welcome',statistics:'stats',debug:'debug'};
const publicRoute={welcome:'welcome',stats:'stats',bot:'config',ai:'ai',providers:'providers',persona:'persona',features:'features',security:'security',connection:'connection',webui:'webui',logs:'logs',chatroom:'chatroom',debug:'debug'};
function splitHashRoute(){let raw=decodeURIComponent((location.hash||'').replace(/^#\/?/,''));if(!raw)return {page:localStorage.webuiPage||'welcome',section:''};const parts=raw.split('#');const page=(parts.shift()||'').replace(/^\//,'')||'welcome';return {page,section:parts.join('#')}}
function normalizeRoutePage(page){return routeAlias[page]||page||'welcome'}
function routeForPage(page,section=''){const base=publicRoute[page]||page||'welcome';return '#/'+base+(section?'#'+encodeURIComponent(section):'')}
const extraRoutes=['chatroom','debug'];
function isValidPage(k){return extraRoutes.includes(k)||pages().some(p=>p.key===k)}
function applyRoute(){const r=splitHashRoute();let page=normalizeRoutePage(r.page);if(pages().length&&!isValidPage(page))page='welcome';if(page==='chatroom'&&state.current!=='chatroom')chatState._enterBottom=true;state.current=page;state.section=r.section||'';localStorage.webuiPage=page}
function updateRoute(page,section='',replace=false){const target=routeForPage(page,section);if(location.hash===target){applyRoute();return}if(replace)history.replaceState(null,'',target);else location.hash=target;applyRoute()}
const meta=()=>pages().find(x=>x.key===state.current)||pages()[0]||{title:'WebUI',desc:''};
function setTheme(t){document.documentElement.dataset.theme=t;localStorage.webuiTheme=t;const btn=el('themeBtn');if(btn)btn.textContent=t==='light'?'☀️ 浅色':'🌙 深色'}
function toggleTheme(){setTheme((document.documentElement.dataset.theme||'dark')==='dark'?'light':'dark')}
function gotoPage(k,section=''){if(state.bundle&&!isValidPage(k))k='welcome';syncCurrentPageFieldsFromDom();if(k==='chatroom')chatState._enterBottom=true;updateRoute(k,section);render()}
function renderNav(){const nav=el('nav');if(nav)nav.innerHTML=pages().map(p=>`<button class="${p.key===state.current?'active':''}" onclick="gotoPage('${p.key}')"><span>${p.icon||'•'}</span><span>${esc(p.title)}</span></button>`).join('')}
function renderConnectionStatus(){const s=state.bundle?.status||{},cs=s.connection_status||{},cfg=s.connection||{};const statusEl=el('connectionStatus'),detailEl=el('connectionDetail');if(statusEl){const text=cs.text||'未知状态';statusEl.textContent=text;statusEl.className='pill '+((cs.state==='connected')?'ok':(cs.state==='failed'||cs.state==='disconnected'||cs.state==='stopped')?'bad':'')}if(detailEl){const lines=[];if(cs.detail)lines.push(cs.detail);const endpoint=[cfg.protocol,cfg.host&&cfg.port?`${cfg.host}:${cfg.port}`:''].filter(Boolean).join(' · ');if(endpoint)lines.push(endpoint);detailEl.textContent=lines.join(' | ')||'暂无连接详情'}}
function render(){if(!state.bundle)return;applyRoute();const logScroll=captureLogScrollState();renderNav();renderConnectionStatus();const m=meta(),titleEl=el('pageTitle'),descEl=el('pageDesc'),brandEl=el('brandName'),contentEl=el('content');const isChat=state.current==='chatroom',isDebug=state.current==='debug';if(titleEl)titleEl.textContent=isChat?'💬 聊天室':isDebug?'🛠️ 调试':(m.icon?m.icon+' ':'')+m.title;if(descEl){const desc=isChat?'':isDebug?'运行资源、缓存和会话状态等可统计的全部调试信息':(Object.prototype.hasOwnProperty.call(m,'desc')?m.desc:'所有数值均可在此直接修改并保存');descEl.textContent=desc;descEl.style.display=desc?'':'none'}if(brandEl)brandEl.textContent=state.bundle.status?.project||'XcBot';if(contentEl)contentEl.innerHTML=isChat?renderChatroom():isDebug?renderDebug():state.current==='welcome'?renderWelcome():state.current==='stats'?renderStats():state.current==='providers'?renderProviders():state.current==='persona'?renderPersona():state.current==='features'?renderFeatures():state.current==='logs'?renderLogs():renderForm(m);if(isChat)afterChatroomRender();if(state.section)requestAnimationFrame(()=>scrollToRouteSection(state.section));if(state.current==='welcome'||state.current==='logs')scheduleLogScrollAfterRender(logScroll)}
function renderWelcome(){const s=state.bundle.status||{},u=s.update||{},ui=s.update_install||{},cs=s.connection_status||{},cc=s.connection||{},ru=s.resource_usage||{},updateBusy=['downloading','extracting','installing','migrating','dependencies','restarting'].includes(ui.state),updatePillClass=u.status==='latest'?'ok':(u.status==='outdated'||u.status==='error'||ui.state==='error')?'bad':'';return `<div class="card"><div class="section-head"><div><h3>运行概览</h3></div><span class="pill ${cs.state==='connected'?'ok':(cs.state==='failed'||cs.state==='disconnected'||cs.state==='stopped')?'bad':''}">${esc(cs.text||'未知状态')}</span></div><div class="mini-stats"><div class="stat"><b>${esc(s.uptime_text||Math.floor((s.uptime_seconds||0)/60)+'分钟')}</b><span>运行时间</span></div><div class="stat"><b>${esc((ru.cpu_percent_normalized??ru.cpu_percent??0)+'% / '+(ru.memory_mb??0)+' MB')}</b><span>CPU/内存占用</span></div><div class="field" style="grid-column:span 2;min-height:auto;display:flex;flex-direction:column;justify-content:center"><div class="label" style="margin-bottom:6px"><span>获取更新</span><span class="pill ${updatePillClass}">${esc(ui.state&&ui.state!=='idle'?ui.text:(u.message||'暂未检查更新'))}</span></div><div class="desc" style="margin-top:0">当前 ${esc(s.version||'--')} / 最新 ${esc(u.latest_version||'--')}</div><div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px"><button class="btn" ${updateBusy?'disabled':''} onclick="checkUpdate()">检查更新</button><button class="btn primary" ${(updateBusy||!u.has_update)?'disabled':''} onclick="installUpdate()">安装更新</button></div><div class="desc">${esc(ui.detail||u.detail||u.release_name||'安装更新会自动下载、覆盖程序、迁移配置并重启。安装后需手动刷新页面。')}</div></div></div></div><div class="card half"><div class="section-head"><div><h3>详细信息</h3><p>环境与启动参数</p></div></div><div class="kv">${[['版本号',(s.project||'')+' '+(s.version||'')],['机器人名',s.bot_name],['运行目录',s.cwd],['Python',s.python],['平台',s.platform],['启动参数',JSON.stringify(s.argv||[])]].map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1])}</div>`).join('')}</div></div><div class="card half"><div class="section-head"><div><h3>连接状态</h3><p>OneBot / Hyper 实时状态</p></div></div><div class="kv">${[['当前状态',cs.text||'未知状态'],['状态详情',cs.detail||'暂无'],['协议',cc.protocol||''],['连接地址',(cc.host&&cc.port)?`${cc.host}:${cc.port}`:''],['监听地址',(cc.listener_host&&cc.listener_port)?`${cc.listener_host}:${cc.listener_port}`:''],['连接模式',cc.mode||'']].map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1])}</div>`).join('')}</div></div><div class="card"><div class="section-head"><div><h3>最近日志</h3></div><div style="display:flex;gap:8px;flex-wrap:wrap">${renderAutoScrollToggle()}<button class="btn" onclick="gotoPage('logs')">查看全部</button></div></div>${renderLogPre(160,'recent')}</div>`}
function chartBars(items,max=0,unit='',color='linear-gradient(135deg,var(--accent),var(--accent2))'){const arr=Array.isArray(items)?items:[];const peak=max||Math.max(1,...arr.map(x=>Number(x.value)||0));return `<div style="display:grid;gap:10px">${arr.length?arr.map(x=>`<div><div style="display:flex;justify-content:space-between;gap:12px;margin-bottom:6px"><span style="font-weight:800">${esc(x.label||x.model||x.session||'-')}</span><span class="pill">${esc((x.value??x.tokens??0)+unit)}</span></div><div style="height:12px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden"><div style="height:100%;width:${Math.max(4,Math.round(((Number(x.value??x.tokens)||0)/peak)*100))}%;background:${color};border-radius:999px"></div></div></div>`).join(''):`<div class="desc">暂无数据</div>`}</div>`}
function chartLine(items,unit=''){const arr=Array.isArray(items)?items:[];if(!arr.length)return '<div class="desc">暂无数据</div>';const values=arr.map(x=>Number(x.value)||0),peak=Math.max(1,...values),width=640,height=170,padX=16,padTop=16,padBottom=28,innerW=width-padX*2,innerH=height-padTop-padBottom;const points=arr.map((x,i)=>{const px=padX+(arr.length===1?innerW/2:(i/(arr.length-1))*innerW);const py=padTop+innerH-(Math.min(Number(x.value)||0,peak)/peak)*innerH;return {x:px,y:py,label:String(x.label||''),value:Number(x.value)||0}});const line=points.map((p,i)=>`${i?'L':'M'}${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(' ');const area=`M${points[0].x.toFixed(2)} ${height-padBottom} `+points.map((p,i)=>`${i?'L':'M'}${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(' ')+` L${points[points.length-1].x.toFixed(2)} ${height-padBottom} Z`;const guides=[0,.25,.5,.75,1].map(r=>{const y=(padTop+innerH-(innerH*r)).toFixed(2);return `<line x1="${padX}" y1="${y}" x2="${width-padX}" y2="${y}" stroke="rgba(255,255,255,.08)" stroke-width="1"/>`}).join('');const markers=points.map(p=>`<circle cx="${p.x.toFixed(2)}" cy="${p.y.toFixed(2)}" r="3.5" fill="var(--accent)" stroke="rgba(255,255,255,.9)" stroke-width="1.2"><title>${esc(p.label)}：${esc(p.value+unit)}</title></circle>`).join('');const labels=points.filter((_,i)=>arr.length<=8||i===0||i===arr.length-1||i%Math.ceil(arr.length/6)===0).map(p=>`<div style="font-size:11px;color:var(--muted);min-width:0;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.label)}</div>`).join('');return `<div style="display:grid;gap:10px"><div style="border:1px solid var(--line2);border-radius:18px;padding:12px;background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.025))"><svg viewBox="0 0 ${width} ${height}" width="100%" height="170" preserveAspectRatio="none" role="img" aria-label="趋势图">${guides}<path d="${area}" fill="rgba(56,213,255,.12)"></path><path d="${line}" fill="none" stroke="var(--accent)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>${markers}</svg><div style="display:grid;grid-template-columns:repeat(${Math.max(2,Math.min(points.filter((_,i)=>arr.length<=8||i===0||i===arr.length-1||i%Math.ceil(arr.length/6)===0).length,6))},minmax(0,1fr));gap:8px;margin-top:8px">${labels}</div></div><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap">${[arr[0],arr[arr.length-1],arr.reduce((a,b)=>Number(a.value)>=Number(b.value)?a:b)].map((x,i)=>`<span class="pill">${esc((i===0?'起点':i===1?'终点':'峰值')+'：'+(x.label||'-')+' '+(x.value||0)+unit)}</span>`).join('')}</div></div>`}
function donutSegments(items){const arr=(Array.isArray(items)?items:[]).filter(x=>Number(x.value)>0);if(!arr.length)return '<div class="desc">暂无数据</div>';const total=arr.reduce((s,x)=>s+(Number(x.value)||0),0)||1;let acc=0;const colors=['#38d5ff','#7cf7c8','#a78bfa','#fb7185','#f59e0b','#60a5fa'];const stops=[];arr.forEach((x,i)=>{const start=Math.round(acc/total*360);acc+=Number(x.value)||0;const end=Math.round(acc/total*360);stops.push(`${colors[i%colors.length]} ${start}deg ${end}deg`)});return `<div style="display:grid;grid-template-columns:180px 1fr;gap:18px;align-items:center"><div style="width:180px;height:180px;margin:auto;border-radius:50%;background:conic-gradient(${stops.join(',')});position:relative;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)"><div style="position:absolute;inset:24px;border-radius:50%;background:rgba(6,21,27,.88);display:grid;place-items:center;text-align:center;font-weight:900">${total}<br><span style="font-size:12px;color:var(--muted)">消息总量</span></div></div><div style="display:grid;gap:10px">${arr.map((x,i)=>`<div style="display:flex;justify-content:space-between;gap:12px;align-items:center"><div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${colors[i%colors.length]}"></span><span>${esc(x.label)}</span></div><span class="pill">${esc(x.value)}</span></div>`).join('')}</div></div>`}
function renderStats(){const st=state.bundle.statistics||{},sum=st.summary||{},models=st.model_ranking||[],top10=st.session_tokens_top10_1d||[],apiHistory=st.api_history||[];return `<div class="card"><div class="section-head"><div><h3>核心指标</h3><p>察看运行质量与模型负载</p></div><button class="btn" onclick="refreshAll(true)">刷新统计</button></div><div class="mini-stats"><div class="stat"><b>${esc(sum.message_count||0)}</b><span>触发消息数</span></div><div class="stat"><b>${esc(sum.api_calls||0)}</b><span>模型调用次数</span></div><div class="stat"><b>${esc(sum.api_tokens||0)}</b><span>累计 Tokens</span></div><div class="stat"><b>${esc(sum.model_count||0)}</b><span>参与模型数</span></div></div></div><div class="card half"><div class="section-head"><div><h3>消息来源占比</h3><p>仅统计真正触发 Bot 的消息</p></div></div>${donutSegments(st.message_scene||[])}</div><div class="card half"><div class="section-head"><div><h3>消息趋势</h3><p>最近 24 个统计时段（折线图）</p></div></div>${chartLine(st.message_trend||[],' 条')}</div><div class="card half"><div class="section-head"><div><h3>模型调用排名及用量</h3><p>按 Tokens 与调用次数综合排序</p></div></div>${chartBars(models.slice(0,8).map(x=>({label:`${x.model} · ${x.calls}次`,value:x.tokens})),0,' Tok','linear-gradient(135deg,var(--accent2),var(--accent3))')}</div><div class="card half"><div class="section-head"><div><h3>最近 1 天会话 Tokens Top 10</h3><p>按会话维度统计 Token 消耗</p></div></div>${chartBars(top10.map(x=>({label:`${x.session} · ${x.calls}次`,value:x.tokens})),0,' Tok','linear-gradient(135deg,#fb7185,#f59e0b)')}</div><div class="card"><div class="section-head"><div><h3>模型调用历史</h3><p>最近 30 条调用记录</p></div></div>${apiHistory.length?`<div style="display:grid;gap:10px">${apiHistory.map(x=>`<div class="field" style="padding:14px"><div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap"><div><div style="font-weight:900">${esc(x.model)} <span class="pill ${x.status==='success'?'ok':x.status==='failed'?'bad':''}">${esc(x.status)}</span></div><div class="desc">${esc(x.scene)} · ${esc(x.host)} · ${esc(x.time)}</div></div><div style="display:flex;gap:8px;flex-wrap:wrap"><span class="pill">${esc(x.message_count||0)} 条消息</span><span class="pill">${esc(x.tokens||0)} Tok</span></div></div><div class="desc" style="margin-top:10px">${esc(x.preview||'')||'（无预览）'}</div></div>`).join('')}</div>`:`<div class="desc">暂无模型调用历史。先让机器人运行一段时间后再来看，会更完整。</div>`}</div><div class="card"><div class="section-head"><div><h3>统计说明</h3><p>当前版本按日志推导统计数据</p></div></div><div class="kv">${[['触发消息数','来自 [API] 请求日志，仅统计真正触发 Bot 的消息'],['模型调用历史','来自 [API] 请求/成功/失败日志'],['模型排名及用量','按模型累计调用次数、成功数、失败数与 Tokens'],['最近 1 天会话 Top10','按 scene 聚合最近 24 小时 Tokens'],['数据更新时间',new Date((st.generated_at||0)*1000).toLocaleString()]].map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1])}</div>`).join('')}</div></div>`}
function dbgKV(rows){return `<div class="kv">${rows.map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1]??'不可用')}</div>`).join('')}</div>`}
function dbgUptime(s){if(s==null)return '--';s=Number(s);const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'天':'')+(h||d?h+'时':'')+m+'分'}
function renderDebug(){const d=state.bundle.status?.debug||{},ru=d.resource_usage||{},rt=d.runtime||{},gc=d.gc||{},web=d.webui||{},conn=d.connection||{};const hasBot=Object.keys(rt).length>0;const procCpu=ru.cpu_percent_process??ru.cpu_percent??0,normCpu=ru.cpu_percent_normalized??ru.cpu_percent??0,sysCpu=ru.cpu_percent_system??'--';const cnt=rt.counters||{};const connStateMap={connected:'已连接',connecting:'连接中',disconnected:'已断开',failed:'失败',starting:'启动中',stopped:'已停止',unknown:'未知'};
  let html=`<div class="card"><div class="section-head"><div><h3>进程资源</h3><p>当前 Python 进程的实时采样</p></div><button class="btn" onclick="refreshAll(true)">刷新调试数据</button></div><div class="mini-stats"><div class="stat"><b>${esc((ru.rss_mb??ru.memory_mb??0)+' MB')}</b><span>RSS 内存</span></div><div class="stat"><b>${esc(normCpu+'%')}</b><span>CPU(整机)</span></div><div class="stat"><b>${esc(procCpu+'%')}</b><span>CPU(单核)</span></div><div class="stat"><b>${esc(ru.threads??'--')}</b><span>线程</span></div><div class="stat"><b>${esc(cnt.uptime_seconds!=null?dbgUptime(cnt.uptime_seconds):'--')}</b><span>运行时长</span></div><div class="stat"><b>${esc(connStateMap[conn.state]||conn.state||'--')}</b><span>WebUI连接</span></div></div></div>`;
  if(!hasBot){html+=`<div class="card"><div class="desc">⚠️ 未检测到运行中的 Bot 主进程（或 WebUI 以独立模式启动）。以下仅显示 WebUI 自身可见的数据，启动 Bot 后将展示功能开关、API/Key、插件、权限、上下文等完整运行时统计。</div>${d.runtime_error?`<pre class="log" style="margin-top:12px">${esc(d.runtime_error)}</pre>`:''}</div>`}
  const rows=[['PID',state.bundle.status?.pid],['进程CPU(整机)',`${normCpu}%`],['进程CPU(单核)',`${procCpu}%`],['系统总CPU',`${sysCpu}%`],['CPU逻辑核心',ru.cpu_count??'--'],['RSS 内存',`${ru.rss_mb??ru.memory_mb??0} MB`],['虚拟内存',`${ru.vms_mb??0} MB`],['线程数',ru.threads??'--'],['打开文件',ru.open_files??'--'],['网络连接',ru.connections??'--'],['日志缓冲',web.log_buffer??0],['GC 对象',gc.tracked_objects??'--'],['GC 计数',(gc.counts||[]).join(' / ')]];
  html+=`<div class="card half"><div class="section-head"><div><h3>运行快照</h3><p>${esc(d.generated_at?new Date(d.generated_at*1000).toLocaleString():'--')}</p></div></div>${dbgKV(rows)}</div>`;
  const ts=rt.token_stats||{},nc=rt.nickname_cache||{},cdb=rt.chat_db||{},mem=rt.ai_memory||{};const cacheRows=[['昵称缓存',nc.items!=null?`${nc.items}${nc.max?' / '+nc.max:''}`:undefined],['chat_db 群数',cdb.groups],['chat_db 历史总数',cdb.history_total],['Token 总量',ts.total_tokens],['Token 会话/用户/群',(ts.sessions!=null)?`${ts.sessions} / ${ts.users} / ${ts.groups}`:undefined],['AI记忆(私/群)',(mem.private!=null)?`${mem.private} / ${mem.group}`:undefined],['普通冷却',cnt.cooldowns],['拍一拍冷却',cnt.poke_cooldowns],['总结记录群',cnt.summary_groups],['正在生成',cnt.generating!=null?(cnt.generating?'是':'否'):undefined],['主循环运行',cnt.running!=null?(cnt.running?'是':'否'):undefined]];
  html+=`<div class="card half"><div class="section-head"><div><h3>缓存 / 计数器</h3><p>定位跑久后变胖的对象</p></div></div>${dbgKV(cacheRows)}</div>`;
  const ak=rt.api_keys;if(ak&&!ak.error){html+=`<div class="card"><div class="section-head"><div><h3>API / Key 状态</h3><p>当前：${esc(ak.current||'--')} ｜ 默认：${esc(ak.default||'--')}</p></div></div><div class="mini-stats"><div class="stat"><b>${esc(ak.total??0)}</b><span>总数</span></div><div class="stat"><b>${esc(ak.active??0)}</b><span>可用</span></div><div class="stat"><b>${esc(ak.cooldown??0)}</b><span>冷却中</span></div><div class="stat"><b>${esc(ak.disabled??0)}</b><span>已禁用</span></div><div class="stat"><b>${esc(ak.multimodal??0)}</b><span>多模态</span></div><div class="stat"><b>${esc(ak.fail_total??0)}</b><span>累计失败</span></div></div>${(ak.items&&ak.items.length)?`<div style="display:grid;gap:8px;margin-top:12px">${ak.items.map(it=>`<div class="field" style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center"><b>${esc(it.api_name||('API-'+it.id))}</b><span class="pill">${esc(it.model||'')}</span><span class="pill">${esc(it.status||'')}</span>${it.fail_count?`<span class="pill" style="border-color:rgba(255,120,120,.4)">失败${esc(it.fail_count)}</span>`:''}${it.is_current?'<span class="pill" style="border-color:rgba(56,213,255,.5)">当前</span>':''}${it.is_default?'<span class="pill">默认</span>':''}</div>`).join('')}</div>`:''}</div>`}else if(ak&&ak.error){html+=`<div class="card"><h3>API / Key 状态</h3><pre class="log">${esc(ak.error)}</pre></div>`}
  const pl=rt.plugins;if(pl){html+=`<div class="card half"><div class="section-head"><div><h3>插件</h3><p>已加载 ${pl.loaded} ｜ 禁用 ${pl.disabled} ｜ 失败 ${pl.failed}</p></div></div>${(pl.loaded_names&&pl.loaded_names.length)?`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${pl.loaded_names.map(n=>`<span class="pill">${esc(n)}</span>`).join('')}</div>`:'<div class="desc">无已加载插件</div>'}${(pl.disabled_names&&pl.disabled_names.length)?`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${pl.disabled_names.map(n=>`<span class="pill" style="opacity:.6">禁用:${esc(n)}</span>`).join('')}</div>`:''}${(pl.failed_names&&pl.failed_names.length)?`<div style="display:flex;flex-wrap:wrap;gap:6px">${pl.failed_names.map(n=>`<span class="pill" style="border-color:rgba(255,120,120,.4)">失败:${esc(n)}</span>`).join('')}</div>`:''}</div>`}
  const pm=rt.permissions;if(pm){html+=`<div class="card half"><div class="section-head"><div><h3>权限 / 名单</h3></div></div>${dbgKV([['ROOT 用户',pm.root],['超级管理员',pm.super],['普通管理员',pm.manage],['黑名单',pm.blacklist]])}</div>`}
  const cs=rt.connection_snapshot;if(cs){html+=`<div class="card half"><div class="section-head"><div><h3>连接快照</h3><p>${rt.hot_switch?'⚠️ 热切换进行中':'稳定'}</p></div></div>${dbgKV([['协议',cs.protocol],['模式',cs.mode],['主机',cs.host+':'+(cs.port||'')],['监听',cs.listener_host+':'+(cs.listener_port||'')],['重试',cs.retries]])}</div>`}
  const cm=rt.cmc;if(cm){const comp=cm.compression||{};html+=`<div class="card half"><div class="section-head"><div><h3>上下文管理器</h3><p>当前内存中加载的会话</p></div></div>${dbgKV([['群上下文',cm.group_contexts],['私聊上下文',cm.private_contexts],['加载消息总数',cm.loaded_messages],['OpenAI客户端池',cm.client_pool],['压缩-会话数',comp.total_sessions],['压缩-总次数',comp.total_compressions],['压缩阈值',comp.threshold],['保留最近',comp.keep_recent]])}</div>`}
  const groups=cdb.top_groups||[];html+=`<div class="card"><div class="section-head"><div><h3>chat_db 群历史</h3><p>按历史条数排序，最多前 20 个群</p></div></div>${groups.length?`<div style="display:grid;gap:10px">${groups.map(g=>`<div class="field" style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap"><b>${esc(g.group)}</b><span class="pill">${esc(g.history)} 条历史</span><span class="pill">${esc(g.tokens)} tokens</span></div>`).join('')}</div>`:'<div class="desc">暂无 chat_db 数据。</div>'}</div>`;
  return html}
function providers(){const v=state.bundle.form_values||{};let ps=Array.isArray(v['Others.llm_providers'])?v['Others.llm_providers']:[];return ps}
function rotation(){const v=state.bundle.form_values||{};return Array.isArray(v['Others.llm_rotation'])?v['Others.llm_rotation']:[]}
function providerModelLabel(pid,m){return (pid?pid+'/':'')+m}
function enabledProviderModels(){const out=[];providers().forEach(p=>(p.models||[]).forEach(m=>{if(m.enabled&&p.id&&m.name)out.push({provider_id:p.id,model:m.name,label:providerModelLabel(p.id,m.name),supports_multimodal:!!m.supports_multimodal})}));return out}
function ensureProviderSelection(){const ps=providers();if(!state.providerIndex||state.providerIndex>=ps.length)state.providerIndex=0;return state.providerIndex||0}
function setProviders(ps){setValue('Others.llm_providers',ps);syncRotationWithEnabled()}
function setRotation(r){setValue('Others.llm_rotation',r)}
function syncRotationWithEnabled(){const enabled=enabledProviderModels(),valid=new Set(enabled.map(x=>x.label)),cur=rotation().filter(x=>valid.has(providerModelLabel(x.provider_id,x.model))),seen=new Set(cur.map(x=>providerModelLabel(x.provider_id,x.model)));enabled.forEach(x=>{if(!seen.has(x.label))cur.push({provider_id:x.provider_id,model:x.model})});setValue('Others.llm_rotation',cur)}
function renderProviders(){const ps=providers(),idx=ensureProviderSelection(),p=ps[idx]||null;return `<div class="card"><div class="section-head"><div><h3>提供商</h3><p>检测模型、勾选模型并设置轮换顺序</p></div><button class="btn primary" onclick="addProvider()">新增提供商</button></div><div style="display:grid;grid-template-columns:260px 1fr;gap:16px"><div style="display:grid;gap:8px;align-content:start">${ps.length?ps.map((x,i)=>`<button class="btn ${i===idx?'primary':''}" style="text-align:left" onclick="syncProviderFieldsFromDom();state.providerIndex=${i};render()">${esc(x.id||('provider'+(i+1)))}</button>`).join(''):'<div class="desc">暂无提供商</div>'}</div><div>${p?renderProviderDetail(p,idx):'<div class="desc">点击新增提供商开始配置。</div>'}</div></div></div>${renderRotationList()}`}
function renderProviderDetail(p,idx){return `<div class="form-grid"><div class="field"><div class="label"><span>提供商 ID</span><button class="btn" onclick="removeProvider(${idx})">删除</button></div><input id="provider_id_${idx}" value="${esc(p.id||'')}" oninput="updateProvider(${idx},'id',this.value.trim())"><div class="desc">Bot 显示名为 提供商ID/模型名</div></div><div class="field"><div class="label"><span>Base URL</span></div><input id="provider_base_${idx}" value="${esc(p.base_url||'')}" placeholder="https://.../v1" oninput="updateProvider(${idx},'base_url',this.value.trim())"></div><div class="field" style="grid-column:1/-1"><div class="label"><span>Keys</span><button class="btn" onclick="detectProviderModels(${idx})">检测模型</button></div><textarea id="provider_keys_${idx}" style="min-height:92px" placeholder="一行一个 key" oninput="updateProvider(${idx},'keys',this.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))">${esc(Array.isArray(p.keys)?p.keys.join('\n'):'')}</textarea><div class="desc" id="providerDetect_${idx}">${esc(p.detect_error||'')}</div></div><div class="field" style="grid-column:1/-1"><div class="label"><span>模型</span><button class="btn" onclick="addProviderModel(${idx})">手动添加模型</button></div><div style="display:grid;gap:8px">${renderProviderModels(p,idx)}</div></div></div>`}
function renderProviderModels(p,idx){const models=Array.isArray(p.models)?p.models:[];if(!models.length)return '<div class="desc">暂无模型。可以检测或手动添加。</div>';return models.map((m,mi)=>`<div class="field" style="display:grid;grid-template-columns:auto 1fr auto auto 120px auto;gap:10px;align-items:center;padding:10px"><input type="checkbox" ${m.enabled?'checked':''} onchange="updateProviderModel(${idx},${mi},'enabled',this.checked)"><input value="${esc(m.name||'')}" oninput="updateProviderModel(${idx},${mi},'name',this.value.trim())"><label style="display:flex;gap:6px;align-items:center"><input type="checkbox" ${m.supports_multimodal?'checked':''} onchange="updateProviderModel(${idx},${mi},'supports_multimodal',this.checked)">多模态</label><span>超时</span><input type="number" step="1" value="${esc(m.timeout_seconds||60)}" oninput="updateProviderModel(${idx},${mi},'timeout_seconds',num(this.value))"><button class="btn" onclick="removeProviderModel(${idx},${mi})">删除</button></div>`).join('')}
function renderRotationList(){const r=rotation(),enabled=enabledProviderModels();return `<div class="card"><div class="section-head"><div><h3>模型轮换列表</h3><p>按顺序轮换</p></div></div><div style="display:grid;gap:8px">${r.length?r.map((x,i)=>`<div class="field" style="display:flex;justify-content:space-between;gap:10px;align-items:center"><b>${esc(providerModelLabel(x.provider_id,x.model))}</b><div><button class="btn" ${i===0?'disabled':''} onclick="moveRotation(${i},-1)">上移</button> <button class="btn" ${i===r.length-1?'disabled':''} onclick="moveRotation(${i},1)">下移</button></div></div>`).join(''):'<div class="desc">勾选模型后会出现在这里。</div>'}</div><div class="desc">可用模型：${esc(enabled.map(x=>x.label).join('，')||'无')}</div></div>`}
function addProvider(){const ps=[...providers(),{id:'provider'+(providers().length+1),base_url:'',keys:[],models:[],detected_models:[]}];state.providerIndex=ps.length-1;setProviders(ps);render()}
function removeProvider(i){if(!confirm('确定删除该提供商？'))return;const ps=[...providers()];ps.splice(i,1);state.providerIndex=0;setProviders(ps);render()}
function updateProvider(i,k,v){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i][k]=v;setProviders(ps)}
function syncProviderFieldsFromDom(){const ps=[...providers()];let changed=false;ps.forEach((p,i)=>{const id=el('provider_id_'+i),base=el('provider_base_'+i),keys=el('provider_keys_'+i);if(id||base||keys){ps[i]=Object.assign({},p||{});if(id)ps[i].id=id.value.trim();if(base)ps[i].base_url=base.value.trim();if(keys)ps[i].keys=keys.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);changed=true}});if(changed)setProviders(ps)}
function updateProviderModel(i,mi,k,v){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].models=[...(ps[i].models||[])];ps[i].models[mi]=Object.assign({},ps[i].models[mi]||{});ps[i].models[mi][k]=v;setProviders(ps)}
function addProviderModel(i){const name=prompt('输入模型名');if(!name)return;const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].models=[...(ps[i].models||[]),{name:name.trim(),enabled:true,supports_multimodal:false,timeout_seconds:60}];setProviders(ps);render()}
function removeProviderModel(i,mi){const ps=[...providers()];ps[i]=Object.assign({},ps[i]||{});ps[i].models=[...(ps[i].models||[])];ps[i].models.splice(mi,1);setProviders(ps);render()}
function moveRotation(i,d){const r=[...rotation()],j=i+d;if(j<0||j>=r.length)return;[r[i],r[j]]=[r[j],r[i]];setRotation(r);render()}
async function detectProviderModels(i){syncProviderFieldsFromDom();syncCurrentPageFieldsFromDom();const ps=providers(),p=ps[i];const box=el('providerDetect_'+i);if(box)box.textContent='检测中...';try{const data=await api('/api/providers/detect-models',{method:'POST',body:JSON.stringify({provider_id:p.id,base_url:p.base_url,keys:p.keys})});const models=data.models||[];const exists=new Set((p.models||[]).map(x=>x.name));p.detected_models=models;p.detect_error=data.error||'';models.forEach(name=>{if(!exists.has(name))(p.models||(p.models=[])).push({name,enabled:false,supports_multimodal:false,timeout_seconds:60})});ps[i]=p;setProviders(ps);render();toast(data.error?('检测失败：'+data.error):('检测到 '+models.length+' 个模型'),!data.error)}catch(e){if(box)box.textContent=e.message;toast(e.message,false)}}
function renderPersona(){const v=state.bundle.form_values||{},presets=Array.isArray(v['Others.personality_presets'])?v['Others.personality_presets']:[],active=v['Others.active_personality_preset']||(presets[0]?.id||'');const cur=presets.find(x=>x.id===active)||presets[0]||{id:'default',name:'默认',prompt:v['Others.personality_prompt']||''};return `<div class="card"><div class="section-head"><div><h3>人格设定</h3><p>选择预设并编辑人设</p></div><button class="btn primary" onclick="addPersonaPreset()">新增预设</button></div><div style="display:grid;grid-template-columns:260px 1fr;gap:16px"><div style="display:grid;gap:8px;align-content:start">${presets.map(p=>`<button class="btn ${p.id===cur.id?'primary':''}" style="text-align:left" onclick="selectPersonaPreset('${esc(p.id)}')">${esc(p.name||p.id)}</button>`).join('')}</div><div class="field"><div class="label"><span>编辑人设</span><span><button class="btn" onclick="renamePersonaPreset('${esc(cur.id)}')">重命名</button> <button class="btn" onclick="removePersonaPreset('${esc(cur.id)}')">删除</button></span></div><textarea style="min-height:420px" oninput="updatePersonaPrompt('${esc(cur.id)}',this.value)">${esc(cur.prompt||'')}</textarea><div class="desc">可使用 {bot_name} 与 {user_name} 占位符</div></div></div></div>`}
function setPersonaPresets(presets,active){setValue('Others.personality_presets',presets);setValue('Others.active_personality_preset',active);const cur=presets.find(x=>x.id===active);if(cur)setValue('Others.personality_prompt',cur.prompt||'')}
function addPersonaPreset(){const presets=[...(state.bundle.form_values['Others.personality_presets']||[])],id='preset'+(presets.length+1);presets.push({id,name:'预设 '+(presets.length+1),prompt:''});setPersonaPresets(presets,id);render()}
function selectPersonaPreset(id){const presets=state.bundle.form_values['Others.personality_presets']||[];setPersonaPresets(presets,id);render()}
function updatePersonaPrompt(id,prompt){const presets=[...(state.bundle.form_values['Others.personality_presets']||[])].map(p=>p.id===id?Object.assign({},p,{prompt}):p);setPersonaPresets(presets,id)}
function renamePersonaPreset(id){const presets=[...(state.bundle.form_values['Others.personality_presets']||[])];const p=presets.find(x=>x.id===id);if(!p)return;const name=prompt('预设名称',p.name||p.id);if(name==null)return;p.name=name.trim()||p.id;setPersonaPresets(presets,id);render()}
function removePersonaPreset(id){const presets=[...(state.bundle.form_values['Others.personality_presets']||[])];if(presets.length<=1){toast('至少保留一个预设',false);return}const next=presets.filter(x=>x.id!==id);setPersonaPresets(next,next[0].id);render()}
function renderForm(m){const v=state.bundle.form_values||{};return `<div class="card" id="section_${esc(m.key||'config')}"><div class="section-head"><div><h3>${esc(m.title)}</h3><p>${esc(m.desc)}</p></div></div><div class="form-grid">${(m.fields||[]).map(f=>renderField(f,v[f.path])).join('')}</div></div>`}
function scrollToRouteSection(section){const safe=String(section||'').replace(/[^a-zA-Z0-9_-]/g,'_');const target=document.getElementById('section_'+safe)||document.querySelector(`[data-section="${CSS.escape(String(section||''))}"]`);if(target)target.scrollIntoView({block:'start',behavior:'smooth'})}
function renderField(f,v,compact=false){const id='f_'+f.path.replace(/[^a-zA-Z0-9]/g,'_');let input='';if(f.type==='bool')input=`<div class="switch ${v?'on':''}" onclick="setBool('${f.path}',this)"></div>`;else if(f.type==='select')input=`<select id="${id}" onchange="setValue('${f.path}',this.value)">${(f.options||[]).map(o=>`<option value="${esc(o)}" ${String(o)===String(v)?'selected':''}>${esc(o)}</option>`).join('')}</select>`;else if(f.type==='multimodal_model')input=renderMultimodalModelSelect(f.path,v,id);else if(f.type==='list')input=`<textarea ${compact?'style="min-height:84px"':''} id="${id}" oninput="setValue('${f.path}',this.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))">${esc(Array.isArray(v)?v.join('\n'):(v??''))}</textarea>`;else if(f.type==='textarea')input=`<textarea ${compact?'style="min-height:84px"':'style="min-height:280px"'} id="${id}" oninput="setValue('${f.path}',this.value)">${esc(v??'')}</textarea>`;else if(f.type==='endpoints')input=renderEndpointsEditor(v||[]);else input=`<input id="${id}" type="${f.type==='password'?'password':f.type==='number'?'number':'text'}" step="any" value="${esc(v??'')}" oninput="setValue('${f.path}',${f.type==='number'?'num(this.value)':'this.value'})">`;return `<div class="field"><div class="label"><span>${esc(f.label)}</span>${f.type==='bool'?input:''}</div>${f.type==='bool'?'':input}<div class="desc">${esc(f.desc||f.path)}</div></div>`}
const num=v=>{const n=Number(v);return Number.isFinite(n)?n:v};
function setValue(path,value){state.bundle.form_values[path]=value;if(path==='manage_users'){state.bundle.manage_users=Array.isArray(value)?value:[];state.bundle.super_users=Array.isArray(value)?value:[]}if(path==='black_list'){state.bundle.blacklist_file=Array.isArray(value)?value:[]}state.dirty=true;state.lastInputAt=Date.now();saveDraft();const save=el('saveState');if(save)save.textContent='有未保存更改'}
function syncCurrentPageFieldsFromDom(){if(!state.bundle)return;if(state.current==='providers')syncProviderFieldsFromDom();const page=meta();const values=state.bundle.form_values||{};(page.fields||[]).forEach(f=>{const id='f_'+f.path.replace(/[^a-zA-Z0-9]/g,'_');if(f.type==='bool'){return}if(f.type==='endpoints'){const arr=Array.isArray(values[f.path])?[...values[f.path]]:[];values[f.path]=arr.map((ep,i)=>{const base=document.getElementById('ep_base_'+i),model=document.getElementById('ep_model_'+i),keys=document.getElementById('ep_keys_'+i),mm=document.getElementById('ep_mm_'+i);return {base_url:base?base.value:(ep.base_url||''),model:model?model.value:(ep.model||''),supports_multimodal:mm?!!mm.checked:!!ep.supports_multimodal,keys:(keys?keys.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean):(Array.isArray(ep.keys)?ep.keys:[]))}});return}const el=document.getElementById(id);if(!el)return;if(f.type==='list')values[f.path]=el.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);else if(f.type==='textarea'||f.type==='text'||f.type==='password'||f.type==='select'||f.type==='multimodal_model')values[f.path]=el.value;else if(f.type==='number')values[f.path]=num(el.value)});state.bundle.form_values=values;const manageUsers=values.manage_users;if(Array.isArray(manageUsers)){state.bundle.manage_users=manageUsers;state.bundle.super_users=manageUsers}const blackList=values.black_list;if(Array.isArray(blackList)){state.bundle.blacklist_file=blackList}}
function setJsonValue(path,txt){try{setValue(path,JSON.parse(txt||'null'))}catch(e){state.dirty=true;const save=el('saveState');if(save)save.textContent='JSON 暂未通过校验'}}
function setBool(path,el){const v=!el.classList.contains('on');el.classList.toggle('on',v);setValue(path,v)}
function shouldAutoRefreshPage(){return state.current==='welcome'||state.current==='logs'||state.current==='stats'||state.current==='debug'}
function renderFeatures(){const map=state.bundle.feature_switches||{},groups={};const fields=(state.bundle.ui_schema.find(x=>x.key==='features')||{}).fields||[];const fieldMap=Object.fromEntries(fields.map(f=>[f.path,f]));(state.bundle.feature_meta||[]).forEach(x=>(groups[x.group]||(groups[x.group]=[])).push(x));return Object.keys(groups).map(g=>{const sid=g.replace(/[^a-zA-Z0-9_-]/g,'_');return `<div class="card" id="section_${sid}" data-section="${esc(g)}"><div class="section-head"><div><h3>${esc(g)}</h3><p>功能配置</p></div><span class="pill">${groups[g].length} 项</span></div><div class="feature-grid">${groups[g].map(it=>{const rel=(featureFieldMap[it.key]||[]).map(p=>fieldMap[p]).filter(Boolean);return `<div class="feature"><h4>${esc(it.title)}</h4><p>${esc(it.desc)}</p><div class="feature-foot"><span class="pill">${esc(it.key)}</span><div class="switch ${map[it.key]?'on':''}" onclick="toggleFeature('${it.key}',this)"></div></div>${rel.length?`<div style="margin-top:12px;display:grid;gap:10px">${rel.map(f=>renderField(f,state.bundle.form_values[f.path],true)).join('')}</div>`:''}</div>`}).join('')}</div></div>`}).join('')}
async function toggleFeature(key,el){state.bundle.feature_switches[key]=!state.bundle.feature_switches[key];el.classList.toggle('on',state.bundle.feature_switches[key]);await saveAll(true)}
async function checkUpdate(){try{const data=await api('/api/update/check',{method:'POST',body:'{}'});if(state.bundle?.status)state.bundle.status.update=data;render();toast(data.message||'已检查更新',true);await refreshAll(true)}catch(e){toast(e.message,false)}}
function scheduleReloadAfterUpdate(){if(state.reloadTimer)return;state.expectedReloadAfterUpdate=true;state.reloadTimer=setInterval(async()=>{try{const r=await fetch('/api/ui-state',{headers:{'X-WebUI-Token':token()},cache:'no-store'});if(!r.ok)return;clearInterval(state.reloadTimer);state.reloadTimer=null;location.reload()}catch(e){}} ,1500)}
async function installUpdate(){if(!confirm('将自动下载并安装最新版本，随后迁移配置并重启，是否继续？'))return;try{const data=await api('/api/update/install',{method:'POST',body:'{}'});if(state.bundle?.status){state.bundle.status.update=data.update||state.bundle.status.update;state.bundle.status.update_install=data.install||state.bundle.status.update_install}state.expectedReloadAfterUpdate=true;render();toast('已开始安装更新，请稍后手动刷新页面',true)}catch(e){toast(e.message,false)}}
function renderLogs(){return `<div class="card"><div class="section-head"><div><h3>实时日志</h3><p>查看实时日志</p></div><div style="display:flex;gap:8px;flex-wrap:wrap">${renderAutoScrollToggle()}<a class="btn" href="/api/raw-log${token()?('?token='+encodeURIComponent(token())):''}" target="_blank">打开完整日志</a></div></div>${renderLogPre(500,'full')}</div>`}
function renderLogPre(limit,name='main'){const logs=(state.bundle.logs||[]).slice(-limit);return `<pre id="log_${name}" class="log ${name==='recent'?'compact':''}">${esc(logs.map(x=>`[${x.time}] [${x.stream}] ${x.message}`).join('\n')||'暂无日志')}</pre>`}
function renderAutoScrollToggle(){const on=localStorage.webuiAutoScroll!=='false';return `<button class="btn" type="button" onclick="toggleAutoScroll()">${on?'✅':'⬜'} 自动滚动</button>`}
function toggleAutoScroll(){const on=!(localStorage.webuiAutoScroll!=='false');localStorage.webuiAutoScroll=String(on);render()}
function captureLogScrollState(){const state={};document.querySelectorAll('pre.log').forEach(x=>{const remain=x.scrollHeight-x.scrollTop-x.clientHeight;state[x.id||'log']={top:x.scrollTop,height:x.scrollHeight,client:x.clientHeight,atBottom:remain<24}});return state}
function restoreLogScrollState(prev={}){const logs=document.querySelectorAll('pre.log');logs.forEach(x=>{const old=prev[x.id||'log'];if(!old||old.atBottom){x.scrollTop=x.scrollHeight;x.scrollTo?.(0,x.scrollHeight)}else{x.scrollTop=Math.min(old.top,x.scrollHeight);x.scrollTo?.(0,Math.min(old.top,x.scrollHeight))}})}
function scheduleLogScrollAfterRender(prev){requestAnimationFrame(()=>{if(localStorage.webuiAutoScroll==='false'){restoreLogScrollState(prev);setTimeout(()=>restoreLogScrollState(prev),80);return}scrollLogsToBottom();setTimeout(scrollLogsToBottom,80);setTimeout(scrollLogsToBottom,260)})}
function scheduleLogAutoScroll(){scheduleLogScrollAfterRender(captureLogScrollState())}
function scrollLogsToBottom(){if(localStorage.webuiAutoScroll==='false')return;const logs=document.querySelectorAll('pre.log');logs.forEach(x=>{x.scrollTop=x.scrollHeight;x.scrollTo?.(0,x.scrollHeight)});if(state.current==='logs'){const full=el('log_full');if(full){full.scrollTop=full.scrollHeight;full.scrollTo?.(0,full.scrollHeight)}const main=document.querySelector('.main');if(main){main.scrollTop=main.scrollHeight;main.scrollTo?.(0,main.scrollHeight)}window.scrollTo(0,Math.max(document.documentElement.scrollHeight,document.body.scrollHeight))}}
function scrollRecentLogsToBottom(){scrollLogsToBottom()}
function getMultimodalModels(){const seen=new Set(),models=[];enabledProviderModels().forEach(x=>{if(x.supports_multimodal&&x.label&&!seen.has(x.label)){seen.add(x.label);models.push(x.label)}});return models}
function renderMultimodalModelSelect(path,v,id){const models=getMultimodalModels();const value=String(v??'');const options=['',...models];if(value&&!models.includes(value))options.push(value);return `<select id="${id}" onchange="setValue('${path}',this.value)">${options.map(o=>`<option value="${esc(o)}" ${String(o)===value?'selected':''}>${esc(o||'不启用转述（保持原行为）')}</option>`).join('')}</select>${models.length?'':`<div class="desc" style="margin-top:8px">先在“提供商”页勾选支持多模态的模型，保存后这里就能选择。</div>`}`}
function renderEndpointsEditor(list){const rows=(Array.isArray(list)?list:[]).map((ep,i)=>`<div class="field" style="padding:12px"><div class="label"><span>接口 #${i+1}</span><button class="btn" type="button" onclick="removeEndpoint(${i})">删除</button></div><input id="ep_base_${i}" placeholder="base_url" value="${esc(ep.base_url||'')}" oninput="updateEndpoint(${i},'base_url',this.value)"><div style="height:8px"></div><input id="ep_model_${i}" placeholder="model" value="${esc(ep.model||'')}" oninput="updateEndpoint(${i},'model',this.value)"><div style="height:8px"></div><label style="display:flex;align-items:center;gap:10px;margin:6px 0 10px 0"><input id="ep_mm_${i}" type="checkbox" ${ep.supports_multimodal?'checked':''} onchange="updateEndpoint(${i},'supports_multimodal',this.checked)"><span>支持多模态</span></label><textarea id="ep_keys_${i}" style="min-height:84px" placeholder="keys，一行一个" oninput="updateEndpoint(${i},'keys',this.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))">${esc(Array.isArray(ep.keys)?ep.keys.join('\n'):'')}</textarea></div>`).join('');return `<div style="display:grid;gap:10px">${rows||'<div class="desc">暂无接口，点击下方按钮新增</div>'}<button class="btn" type="button" onclick="addEndpoint()">新增大模型接口</button></div>`}
function updateEndpoint(index,key,value){const arr=Array.isArray(state.bundle.form_values['Others.llm_endpoints'])?[...state.bundle.form_values['Others.llm_endpoints']]:[];arr[index]=Object.assign({base_url:'',model:'',keys:[],supports_multimodal:false},arr[index]||{});arr[index][key]=value;setValue('Others.llm_endpoints',arr)}
function addEndpoint(){const arr=Array.isArray(state.bundle.form_values['Others.llm_endpoints'])?[...state.bundle.form_values['Others.llm_endpoints']]:[];arr.push({base_url:'',model:'',keys:[],supports_multimodal:false});setValue('Others.llm_endpoints',arr);render()}
function removeEndpoint(index){const arr=Array.isArray(state.bundle.form_values['Others.llm_endpoints'])?[...state.bundle.form_values['Others.llm_endpoints']]:[];arr.splice(index,1);setValue('Others.llm_endpoints',arr);render()}
async function saveAll(silent=false){if(!state.bundle||state.saving)return;state.saving=true;const save=el('saveState');if(save)save.textContent='正在保存...';try{syncCurrentPageFieldsFromDom();saveDraft();const manageUsers=state.bundle.form_values?.manage_users??state.bundle.manage_users;const blackList=state.bundle.form_values?.black_list??state.bundle.blacklist_file;const saved=await api('/api/ui-state',{method:'POST',body:JSON.stringify({form_values:state.bundle.form_values||{},feature_switches:state.bundle.feature_switches||{},super_users:manageUsers,manage_users:manageUsers,blacklist_file:blackList})});clearDraft();state.bundle=saved;state.dirty=false;render();await refreshAll(true);if(!silent)toast('设置已保存并已从 config 重新同步',true)}catch(e){toast(e.message,false)}finally{state.saving=false}}
function isEditingField(){const el=document.activeElement;return !!(el&&['INPUT','TEXTAREA','SELECT'].includes(el.tagName))}
function shouldPauseAutoRefresh(){return !!(state.dirty||isEditingField()||(Date.now()-(state.lastInputAt||0)<15000))}
async function refreshAll(force=false){if(!force&&!shouldAutoRefreshPage())return;if(shouldPauseAutoRefresh()&&!force)return;try{const data=await api('/api/ui-state');state.apiFailCount=0;if(shouldPauseAutoRefresh()&&!force)return;state.bundle=data;applyDraft(state.bundle);const installState=data?.status?.update_install?.state||'';if(['restarting','idle'].includes(installState)&&state.expectedReloadAfterUpdate){scheduleReloadAfterUpdate()}render();const save=el('saveState');if(save&&!state.dirty)save.textContent='已同步 '+new Date().toLocaleTimeString()}catch(e){state.apiFailCount=(state.apiFailCount||0)+1;if(state.expectedReloadAfterUpdate&&state.apiFailCount>=2)scheduleReloadAfterUpdate();if(!state.expectedReloadAfterUpdate)toast(e.message,false)}}
let chatState={sessions:[],current:null,session:null,models:[],loading:false,sending:false,stream:localStorage.webuiChatStream!=='0',attachments:[],controller:null};
async function chatApi(path,opt={}){opt.headers=Object.assign({'Content-Type':'application/json','X-WebUI-Token':token()},opt.headers||{});const r=await fetch(path,opt),j=await r.json().catch(()=>({ok:false,error:'请求失败'}));if(r.status===401){localStorage.removeItem('webuiToken');location.href='/auth/login';throw new Error('请先登录')}if(!j.ok)throw new Error(j.error||'请求失败');return j.data??j}
function renderChatroom(){return `<div class="chatroom"><div class="chat-side"><button class="btn primary" onclick="chatNew()">＋ 新建会话</button><div class="sess-list" id="sessList">${renderSessList()}</div></div><div class="chat-main"><div class="chat-head"><select id="chatModel" onchange="chatPickModel(this.value)">${renderModelOptions()}</select><span class="pill" style="margin-left:auto">${chatState.session?esc(chatState.session.title||'新会话'):'未选择会话'}</span></div><div class="chat-msgs" id="chatMsgs">${renderChatMsgs()}</div><div class="chat-composer"><input id="chatFileInput" type="file" multiple accept="image/*,.txt,.md,.json,.csv,.log,.py,.js,.ts,.html,.css" style="display:none" onchange="chatPickFiles(this.files);this.value=''">${renderChatFiles()}<div class="chat-input"><button class="chat-icon" title="上传文件/图片" onclick="el('chatFileInput')?.click()">＋</button><textarea id="chatInput" placeholder="输入消息，Enter 发送，Shift+Enter 换行" oninput="chatAutoGrow(this)" onpaste="chatPaste(event)" onkeydown="chatInputKey(event)" ${chatState.session?'':'disabled'}></textarea><button class="chat-icon ${chatState.stream?'on':''}" title="${chatState.stream?'流式输出已开启':'流式输出已关闭'}" onclick="chatToggleStream()">~</button><button class="btn primary chat-send ${chatState.sending?'stop':''}" id="chatSendBtn" onclick="${chatState.sending?'chatStop()':'chatSend()'}" ${chatState.session?'':'disabled'} title="${chatState.sending?'停止':'发送'}">${chatState.sending?'■':'↑'}</button></div></div></div></div>`}
function renderModelOptions(){const ms=chatState.models||[];if(!ms.length)return '<option value="">未配置模型</option>';const cur=chatState.session?.model||ms[0].model;return ms.map(m=>`<option value="${esc(m.model)}" ${m.model===cur?'selected':''}>${esc(m.model)}${m.supports_multimodal?' 👁':''}</option>`).join('')}
function renderSessList(){const ss=chatState.sessions||[];if(!ss.length)return '<div class="desc" style="padding:8px">暂无会话，点击上方新建</div>';return ss.map(s=>`<div class="sess-item ${s.id===chatState.current?'active':''}" onclick="chatOpen('${s.id}')"><div class="t">${esc(s.title||'新会话')}</div><div class="m">${esc(s.model||'-')} · ${s.message_count||0} 次</div>${s.id===chatState.current?`<div class="sess-actions"><button class="btn" onclick="event.stopPropagation();chatRename('${s.id}')">重命名</button><button class="btn" onclick="event.stopPropagation();chatDelete('${s.id}')">删除</button></div>`:''}</div>`).join('')}
function copyMdCode(btn){const code=btn.closest('.md-code')?.querySelector('code')?.innerText||'';navigator.clipboard?.writeText(code).then(()=>{const old=btn.textContent;btn.textContent='已复制';setTimeout(()=>btn.textContent=old,900)}).catch(()=>toast('复制失败',false))}
function mdInline(t){return esc(t).replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noreferrer">$1</a>').replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>')}
function mdCells(line){let x=line.trim();if(x.startsWith('|'))x=x.slice(1);if(x.endsWith('|'))x=x.slice(0,-1);return x.split('|').map(v=>v.trim())}
function isMdTableSep(line){return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line||'')}
function chatMarkdown(s){s=String(s||'').replace(/\r\n/g,'\n');const lines=s.split('\n'),out=[];let i=0;function para(buf){if(buf.length)out.push('<p>'+mdInline(buf.join(' ').trim())+'</p>')}while(i<lines.length){let line=lines[i];if(!line.trim()){i++;continue}if(/^```/.test(line.trim())){const lang=line.trim().replace(/^```/,'').trim()||'code',buf=[];i++;while(i<lines.length&&!/^```/.test(lines[i].trim()))buf.push(lines[i++]);if(i<lines.length)i++;out.push(`<div class="md-code"><div class="md-code-head"><span>${esc(lang)}</span><button class="md-copy" onclick="copyMdCode(this)">复制</button></div><pre><code>${esc(buf.join('\n'))}</code></pre></div>`);continue}if(line.includes('|')&&lines[i+1]&&isMdTableSep(lines[i+1])){const heads=mdCells(line),rows=[];i+=2;while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()){rows.push(mdCells(lines[i++]));}out.push(`<div class="md-table-wrap"><table><thead><tr>${heads.map(h=>`<th>${mdInline(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${heads.map((_,idx)=>`<td>${mdInline(r[idx]||'')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);continue}if(/^---+$/.test(line.trim())){out.push('<hr>');i++;continue}const hm=line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);if(hm){const lv=Math.min(3,hm[1].length);out.push(`<h${lv}>${mdInline(hm[2])}</h${lv}>`);i++;continue}if(/^>\s?/.test(line)){const buf=[];while(i<lines.length&&/^>\s?/.test(lines[i]))buf.push(lines[i++].replace(/^>\s?/,''));out.push('<blockquote>'+mdInline(buf.join('\n')).replace(/\n/g,'<br>')+'</blockquote>');continue}if(/^\s*[-*+]\s+/.test(line)){const items=[];while(i<lines.length&&/^\s*[-*+]\s+/.test(lines[i]))items.push('<li>'+mdInline(lines[i++].replace(/^\s*[-*+]\s+/,''))+'</li>');out.push('<ul>'+items.join('')+'</ul>');continue}if(/^\s*\d+[.)]\s+/.test(line)){const items=[];while(i<lines.length&&/^\s*\d+[.)]\s+/.test(lines[i]))items.push('<li>'+mdInline(lines[i++].replace(/^\s*\d+[.)]\s+/,''))+'</li>');out.push('<ol>'+items.join('')+'</ol>');continue}const buf=[];while(i<lines.length&&lines[i].trim()&&!/^(\s{0,3}#{1,6})\s+/.test(lines[i])&&!/^```/.test(lines[i].trim())&&!/^---+$/.test(lines[i].trim())&&!/^>\s?/.test(lines[i])&&!/^\s*[-*+]\s+/.test(lines[i])&&!/^\s*\d+[.)]\s+/.test(lines[i])&&!(lines[i].includes('|')&&lines[i+1]&&isMdTableSep(lines[i+1])))buf.push(lines[i++]);para(buf)}return '<div class="md">'+out.join('')+'</div>'}
function renderChatMsgs(){const s=chatState.session;if(!s)return '<div class="chat-empty">选择左侧会话，或新建一个开始对话</div>';const msgs=(s.messages||[]).filter(m=>m.role==='user'||m.role==='assistant');if(!msgs.length&&!chatState.sending)return '<div class="chat-empty">发送第一条消息开始对话吧</div>';return msgs.map(m=>`<div class="msg ${m.role}"><div class="av">${m.role==='user'?'🧑':'🤖'}</div><div class="bubble">${chatMarkdown(m.content||'')}</div></div>`).join('')+(chatState.sending?'<div class="msg assistant"><div class="av">🤖</div><div class="bubble" id="chatPartial">'+chatMarkdown(chatState.partial||'思考中…')+'</div></div>':'')}
function renderChatFiles(){const fs=chatState.attachments||[];if(!fs.length)return '<div class="chat-files" id="chatFiles" style="display:none"></div>';return `<div class="chat-files" id="chatFiles">${fs.map((f,i)=>`<span class="chat-file" title="${esc(f.name)}">${f.type&&f.type.startsWith('image/')?'🖼':'📎'} ${esc(f.name)}<button onclick="chatRemoveFile(${i})">×</button></span>`).join('')}</div>`}
function chatNearBottom(){const box=el('chatMsgs');return !box||(box.scrollHeight-box.scrollTop-box.clientHeight)<80}
function chatRender(stick){const box=el('chatMsgs');chatState._stickBottom=stick??chatNearBottom();chatState._restoreScroll=(!chatState._stickBottom&&box)?box.scrollTop:null;render()}
function afterChatroomRender(){const box=el('chatMsgs');if(box){if(chatState._stickBottom||chatState._enterBottom){box.scrollTop=box.scrollHeight;box.scrollTo?.(0,box.scrollHeight);if(chatState._enterBottom)setTimeout(()=>{box.scrollTop=box.scrollHeight;box.scrollTo?.(0,box.scrollHeight)},80)}else if(chatState._restoreScroll!=null)box.scrollTop=chatState._restoreScroll}chatState._enterBottom=false;chatState._stickBottom=false;chatState._restoreScroll=null;if(!chatState._loaded){chatState._loaded=true;chatBootstrap();return}const input=el('chatInput');if(input&&!input.disabled){chatAutoGrow(input)}}
async function chatBootstrap(){try{const[models,sessions]=await Promise.all([chatApi('/api/chat/models'),chatApi('/api/chat/sessions')]);chatState.models=models;chatState.sessions=sessions;if(!chatState.current){if(sessions.length){await chatOpen(sessions[0].id);return}await chatNew();return}render()}catch(e){toast(e.message,false)}}
async function chatRefreshSessions(){try{chatState.sessions=await chatApi('/api/chat/sessions')}catch(e){}}
async function chatOpen(id){try{chatState.current=id;chatState.session=await chatApi('/api/chat/session?id='+encodeURIComponent(id));if(state.current==='chatroom')chatRender(true)}catch(e){toast(e.message,false)}}
async function chatNew(){try{const model=(chatState.models[0]&&chatState.models[0].model)||'';const obj=await chatApi('/api/chat/new',{method:'POST',body:JSON.stringify({model})});chatState.current=obj.id;chatState.session=obj;chatState.attachments=[];await chatRefreshSessions();chatRender(true)}catch(e){toast(e.message,false)}}
function chatPickModel(model){if(chatState.session)chatState.session.model=model}
function chatAutoGrow(ta){if(!ta)return;ta.style.height='38px';ta.style.height=Math.min(180,Math.max(38,ta.scrollHeight))+'px'}
function chatToggleStream(){chatState.stream=!chatState.stream;localStorage.webuiChatStream=chatState.stream?'1':'0';chatRender(false)}
function chatInputKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();chatSend()}}
function chatStop(){if(chatState.controller){try{chatState.controller.abort()}catch(e){}}chatState.controller=null;chatState.sending=false;chatState.partial='';chatRender(false)}
function chatRemoveFile(i){chatState.attachments.splice(i,1);chatRender(false)}
async function chatFileToAttachment(f){if(f.size>8*1024*1024){toast('文件过大：'+f.name,false);return null}const item={name:f.name||('paste-'+Date.now()+((f.type||'').startsWith('image/')?'.png':'')),type:f.type||'application/octet-stream',size:f.size};if((f.type||'').startsWith('image/')){item.data=await new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(String(r.result||''));r.onerror=()=>rej(r.error);r.readAsDataURL(f)})}else if((f.type||'').startsWith('text/')||/\.(txt|md|json|csv|log|py|js|ts|html|css)$/i.test(item.name)){item.text=await f.text()}return item}
async function chatPickFiles(files){for(const f of Array.from(files||[]).slice(0,8-chatState.attachments.length)){const item=await chatFileToAttachment(f);if(item)chatState.attachments.push(item)}chatRender(false)}
async function chatPaste(e){const items=Array.from(e.clipboardData?.items||[]),files=[];for(const it of items){if(it.kind==='file'){const f=it.getAsFile();if(f)files.push(f)}}if(files.length){e.preventDefault();await chatPickFiles(files)}}
async function chatSend(){if(!chatState.session)return;if(chatState.sending){chatStop();return}const ta=el('chatInput');const text=(ta?.value||'').trim();if(!text&&!chatState.attachments.length)return;const model=el('chatModel')?.value||chatState.session.model||'';const attachments=chatState.attachments.slice();chatState.session.messages=chatState.session.messages||[];chatState.session.messages.push({role:'user',content:text+(attachments.length?('\n'+attachments.map(a=>'[附件：'+a.name+']').join('\n')):''),attachments});if(ta){ta.value='';chatAutoGrow(ta)}chatState.attachments=[];chatState.sending=true;chatState.partial='';chatState.controller=new AbortController();chatRender(true);try{if(chatState.stream){await chatSendStream(model,text,attachments)}else{const data=await chatApi('/api/chat/send',{method:'POST',signal:chatState.controller.signal,body:JSON.stringify({id:chatState.current,model,text,attachments})});chatState.session=data.session}chatState.sending=false;chatState.controller=null;chatState.partial='';await chatRefreshSessions();chatRender(chatNearBottom())}catch(e){if(e.name!=='AbortError')toast(e.message,false);chatState.sending=false;chatState.controller=null;chatState.partial='';await chatRefreshSessions();chatRender(chatNearBottom())}}
function chatUpdatePartial(force=false){const node=el('chatPartial');if(!node)return;if(!force){const now=Date.now();if(chatState._partialTimer||now-(chatState._partialAt||0)<100){if(!chatState._partialTimer)chatState._partialTimer=setTimeout(()=>{chatState._partialTimer=null;chatUpdatePartial(true)},100);return}}const stick=chatNearBottom();chatState._partialAt=Date.now();node.innerHTML=chatMarkdown(chatState.partial||'思考中…');const box=el('chatMsgs');if(box&&stick)box.scrollTop=box.scrollHeight}
async function chatSendStream(model,text,attachments){const r=await fetch('/api/chat/send-stream',{method:'POST',headers:{'Content-Type':'application/json','X-WebUI-Token':token()},signal:chatState.controller.signal,body:JSON.stringify({id:chatState.current,model,text,attachments})});if(r.status===401){localStorage.removeItem('webuiToken');location.href='/auth/login';throw new Error('请先登录')}if(!r.ok)throw new Error('请求失败：'+r.status);const reader=r.body.getReader(),dec=new TextDecoder('utf-8');let buf='';while(true){const {value,done}=await reader.read();if(done)break;buf+=dec.decode(value,{stream:true});const chunks=buf.split('\n\n');buf=chunks.pop()||'';for(const raw of chunks){let ev='message',data='';raw.split('\n').forEach(line=>{if(line.startsWith('event:'))ev=line.slice(6).trim();else if(line.startsWith('data:'))data+=line.slice(5).trim()});if(!data)continue;const obj=JSON.parse(data);if(ev==='delta'){chatState.partial=(chatState.partial||'')+(obj.text||'');chatUpdatePartial(false)}else if(ev==='done'){chatState.session=obj.session;chatUpdatePartial(true);return}else if(ev==='error'){throw new Error(obj.error||'请求失败')}}}}
async function chatRename(id){const name=prompt('输入新的会话标题');if(name==null)return;try{const obj=await chatApi('/api/chat/rename',{method:'POST',body:JSON.stringify({id,title:name})});if(chatState.current===id)chatState.session=obj;await chatRefreshSessions();render()}catch(e){toast(e.message,false)}}
async function chatDelete(id){if(!confirm('确定删除该会话？此操作不可恢复。'))return;try{await chatApi('/api/chat/delete',{method:'POST',body:JSON.stringify({id})});if(chatState.current===id){chatState.current=null;chatState.session=null}await chatRefreshSessions();render()}catch(e){toast(e.message,false)}}
setTheme(localStorage.webuiTheme||'dark');window.addEventListener('hashchange',()=>{applyRoute();render()});applyRoute();if(state.current==='chatroom')chatState._enterBottom=true;if(!location.hash)updateRoute(state.current,'',true);refreshAll(true);setInterval(()=>refreshAll(false),3000);setInterval(()=>{if(state.current==='welcome'||state.current==='logs')scrollLogsToBottom()},500);
</script></body></html>'''


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