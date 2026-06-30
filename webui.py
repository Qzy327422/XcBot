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
import base64
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

def _log_file(date: Optional[str] = None) -> Path:
    """返回当天（或指定日期）的日志文件路径，格式 runtime-YYYY-MM-DD.log"""
    d = date or datetime.now().strftime("%Y-%m-%d")
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
_statistics_cache: Dict[str, Any] = {"timestamp": 0.0, "data": None}
_statistics_cache_lock = threading.Lock()
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
_net_connections_cache: Dict[str, Any] = {"value": 0, "ts": 0.0}
_open_files_cache: Dict[str, Any] = {"value": 0, "ts": 0.0}

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
            # net_connections 在连接数多时极慢，缓存10秒
            now = time.time()
            if now - _net_connections_cache["ts"] > 10:
                _net_connections_cache["value"] = len(_psutil_proc.net_connections(kind='inet'))
                _net_connections_cache["ts"] = now
            now2 = time.time()
            if now2 - _open_files_cache["ts"] > 10:
                _open_files_cache["value"] = len(_psutil_proc.open_files())
                _open_files_cache["ts"] = now2
            usage.update({
                "rss_mb": round(info.rss / 1024 / 1024, 1),
                "vms_mb": round(info.vms / 1024 / 1024, 1),
                "threads": _psutil_proc.num_threads(),
                "open_files": _open_files_cache["value"],
                "connections": _net_connections_cache["value"],
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
            continue
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
    if not providers:
        providers = [{"id": "provider1", "base_url": "", "keys": [], "models": [], "detected_models": []}]
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
            model = str(ep.get("model", "") or "").strip()
            if not model:
                continue
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
            field("Others.llm_split.filter_regex", "内容过滤正则表达式", "textarea", "模式二使用。对每段文本做清理，例如移除换行：\\n|\\r"),
            field("Others.llm_split.max_chars_no_split", "超过多少字不分段", "number", "最终要发送的整条内容超过[ ]字时，忽略 <split>/正则分段，改为单条发送；填 0 表示不限制", 0),
        ]},
        {"key": "providers", "title": "提供商", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg>', "desc": "配置提供商、检测模型并设置轮换顺序", "fields": [
            field("Others.llm_providers", "提供商", "providers"),
            field("Others.llm_rotation", "模型轮换", "rotation"),
            field("Others.api_multimodal_model", "多模态图片模型", "multimodal_model", "主模型不支持多模态且用户发送图片时使用的多模态模型"),
            field("Others.api_multimodal_image_mode", "图片处理模式", "select", "relay=多模态模型先转述图片，再交给主模型回复；direct=直接由多模态模型回复图片消息", "relay", ["relay", "direct"]),
        ]},
        {"key": "persona", "title": "人格设定", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>', "desc": "编辑人设", "fields": [
            field("Others.personality_presets", "人格预设", "persona_presets"),
            field("Others.active_personality_preset", "当前预设", "text"),
            field("Others.personality_prompt", "编辑人设", "textarea", "可使用 {bot_name} 与 {user_name} 占位符"),
        ]},
        {"key": "features", "title": "功能配置", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="7" height="7" x="14" y="3" rx="1"/><path d="M10 21V8a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5a1 1 0 0 0-1-1H3"/></svg>', "desc": "配置功能", "fields": [
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
            field("WebUI.background_image", "自定义背景图片", "text", "填写本地图片路径或图片 URL；留空使用主题背景"),
            field("WebUI.background_blur", "背景模糊度", "number", "0 到 40，数值越大越柔和", 10, min=0, max=40),
            field("WebUI.liquid_glass", "仿苹果液体玻璃 UI", "bool", "⚠️ 低配机可能会出现卡顿，请自行决定是否开启"),
        ]},
        {"key": "logs", "title": "实时日志", "icon": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>', "desc": "查看完整运行日志", "fields": []},
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
    today = _log_file()
    if today.exists():
        lines = today.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        return [{"time": "", "stream": "file", "message": line} for line in lines]
    return []


def _iter_runtime_log_lines(limit: int = 20000) -> list[str]:
    today = _log_file()
    if not today.exists():
        return []
    try:
        return today.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return []


def _parse_log_timestamp(text: str) -> Optional[int]:
    try:
        return int(datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return None


def collect_statistics() -> Dict[str, Any]:
    # ponytail: 读40000行日志做regex，每3s触发一次会OOM，缓存60s
    now = int(time.time())
    with _statistics_cache_lock:
        if _statistics_cache["data"] is not None and now - _statistics_cache["timestamp"] < 60:
            return _statistics_cache["data"]
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
    with _statistics_cache_lock:
        _statistics_cache["data"] = result
        _statistics_cache["timestamp"] = now
    return result


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
            "admin": _safe_len(getattr(main_mod, "ROOT_User", [])),
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


def _store_registry() -> list:
    """下载仓库 zip，从每个插件的 metadata.yaml 构建列表，合并本地已安装插件"""
    zip_url = f"https://github.com/{PLUGIN_STORE_REPO}/archive/refs/heads/main.zip"
    raw = None
    for url in _github_accelerated_urls(zip_url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "XcBot-WebUI"})
            with _make_opener().open(req, timeout=20) as r:
                raw = r.read()
            break
        except Exception:
            pass
    data = []
    if raw:
        repo_short = PLUGIN_STORE_REPO.split("/")[-1]
        prefix = f"{repo_short}-main/plugins/"
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
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


def _store_install(name: str, path: str) -> str:
    """下载并解压单个插件到 plugins/ 目录，返回安装结果描述"""
    if not name or "/" in name or ".." in name:
        raise ValueError("无效的插件名")
    zip_url = f"https://github.com/{PLUGIN_STORE_REPO}/archive/refs/heads/main.zip"
    urls = _github_accelerated_urls(zip_url)
    last_err = ""
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "XcBot-WebUI"})
            with _make_opener().open(req, timeout=30) as r:
                data = r.read()
            break
        except Exception as e:
            last_err = str(e)
            data = None
    if not data:
        raise RuntimeError(f"下载插件仓库失败：{last_err}")

    PLUGIN_DIR.mkdir(exist_ok=True)
    repo_short = PLUGIN_STORE_REPO.split("/")[-1]
    prefix = f"{repo_short}-main/{path.strip('/')}/"
    dest = PLUGIN_DIR / name
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [m for m in zf.namelist() if m.startswith(prefix) and not m.endswith("/")]
        if not members:
            raise RuntimeError(f"zip 中找不到路径 {prefix}")
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for m in members:
            rel = m[len(prefix):]
            if not rel:
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(m))

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
    if not resolved.exists() or not resolved.is_file() or resolved.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        _json_response(handler, {"ok": False, "error": "背景图片不存在或格式不支持"}, 404)
        return
    content_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(resolved.suffix.lower(), "application/octet-stream")
    _binary_response(handler, resolved.read_bytes(), content_type)


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
        return

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
            elif parsed.path.startswith("/static/"):
                fname = parsed.path[len("/static/"):]
                fpath = (BASE_DIR / "static" / fname).resolve()
                if fpath.is_file() and str(fpath).startswith(str((BASE_DIR / "static").resolve())):
                    ct = "text/javascript" if fname.endswith(".js") else "text/css"
                    _text_response(self, fpath.read_text(encoding="utf-8"), ct + "; charset=utf-8", cache="public, max-age=31536000, immutable")
                else:
                    _json_response(self, {"ok": False, "error": "Not Found"}, 404)
            elif parsed.path == "/api/webui/background":
                _configured_background_image_response(self)
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
                    with _make_opener().open(req, timeout=20) as resp:
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
                if not name or "/" in name or ".." in name:
                    raise ValueError("无效的插件名")
                raw = base64.b64decode(zip_b64)
                dest = PLUGIN_DIR / name
                PLUGIN_DIR.mkdir(exist_ok=True)
                if dest.exists():
                    shutil.rmtree(dest)
                dest.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    for m in zf.namelist():
                        if m.endswith("/"):
                            continue
                        parts = m.split("/", 1)
                        rel = parts[1] if len(parts) > 1 else parts[0]
                        out = (dest / rel).resolve()
                        if not str(out).startswith(str(dest.resolve())):
                            continue  # 防止路径穿越
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(zf.read(m))
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
<body><svg style="position:absolute;width:0;height:0;pointer-events:none" aria-hidden="true"><defs><filter id="xcbot-liquid-glass" x="-10%" y="-10%" width="120%" height="120%" primitiveUnits="userSpaceOnUse" color-interpolation-filters="sRGB"><feTurbulence type="fractalNoise" baseFrequency="0.018 0.014" numOctaves="4" seed="7" result="noise"/><feDisplacementMap in="SourceGraphic" in2="noise" scale="20" xChannelSelector="R" yChannelSelector="G"/></filter></defs></svg><div class="app"><aside class="sidebar"><div class="brand"><div class="logo"><img src="/assets/icon.jpg" alt="XcBot"></div><div><h1 id="brandName">XcBot</h1><p>实时 Web 管理台</p></div></div><div class="nav-title">功能列表</div><nav id="nav" class="nav"></nav><div class="nav-title">OneBot / Hyper 连接状态</div><div id="connectionStatus" class="pill">加载中...</div><div id="connectionDetail" class="desc" style="margin:10px 12px 0 12px"></div></aside><main class="main"><div class="topbar"><div class="title"><h2 id="pageTitle">加载中...</h2><p id="pageDesc">正在连接 WebUI</p></div><div class="toolbar"><span id="saveState" class="pill">未加载</span><button class="btn" onclick="gotoPage('chatroom')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px;vertical-align:-2px;margin-right:5px"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>聊天室</button><button class="btn" onclick="gotoPage('debug')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px;vertical-align:-2px;margin-right:5px"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>调试</button><button class="btn" id="themeBtn" onclick="toggleTheme()">深色</button><button class="btn primary" onclick="saveAll()">保存设置</button></div></div><section id="content" class="grid"></section></main></div><div id="toast" class="toast"></div><div id="submitModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;align-items:center;justify-content:center"><div style="max-width:420px;width:90%;padding:28px 32px;border-radius:var(--radius,26px);background:var(--bg2);border:1px solid var(--line);box-shadow:0 24px 90px rgba(0,0,0,.4)"><h3 style="margin:0 0 16px;color:var(--text)">提交插件</h3><div style="display:grid;gap:10px"><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px"><div><div class="label">插件名</div><input class="input" id="submitName" placeholder="your_plugin"></div><div><div class="label">作者</div><input class="input" id="submitAuthor" placeholder="你的名字"></div></div><div><div class="label">功能描述</div><textarea class="input" id="submitDesc" rows="3" placeholder="简单描述插件功能" style="resize:vertical"></textarea></div><p class="desc">提交后将打开 GitHub Issue 页面，把 zip 拖入评论框上传后点提交</p><div style="display:flex;gap:10px;justify-content:flex-end"><button class="btn" onclick="el('submitModal').style.display='none'">取消</button><button class="btn primary" onclick="storeSubmit()">打开 GitHub Issue</button></div></div></div></div><div id="leaveModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;align-items:center;justify-content:center"><div style="max-width:360px;width:90%;padding:28px 32px;border-radius:var(--radius,26px);background:var(--bg2);border:1px solid var(--line);box-shadow:0 24px 90px rgba(0,0,0,.4)"><h3 style="margin:0 0 8px;color:var(--text)">确认操作</h3><p style="margin:0 0 24px;color:var(--muted)">当前页面有未保存修改，离开后将丢失这些更改。是否离开？</p><div style="display:flex;gap:10px;justify-content:flex-end"><button class="btn" onclick="leaveCancel()">取消</button><button class="btn primary" onclick="leaveConfirm()">确定</button></div></div></div><input id="pluginUploadInput" type="file" accept=".zip" style="display:none" onchange="storeUploadFile(this)"><button id="pluginUploadBtn" onclick="el('pluginUploadInput').click()" title="上传本地插件" style="display:none;position:fixed;right:24px;bottom:24px;width:48px;height:48px;border-radius:50%;background:var(--accent,#6366f1);border:none;cursor:pointer;font-size:22px;color:#fff;box-shadow:0 2px 8px #0004;z-index:999">&#8679;</button>
<script src="/static/app.js"></script></body></html>'''

# 注入版本号到静态资源 URL，实现缓存破坏：版本变了浏览器自动重新下载
_sv = ((read_json(CONFIG_PATH).get("Others") or {}).get("version_name") or "1").strip().replace(" ", "")
INDEX_HTML = INDEX_HTML.replace('/static/app.css"', f'/static/app.css?v={_sv}"') \
                       .replace('/static/app.js"', f'/static/app.js?v={_sv}"')
LOGIN_HTML = LOGIN_HTML.replace('/static/login.js"', f'/static/login.js?v={_sv}"')


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