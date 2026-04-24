# -*- coding: utf-8 -*-
"""XcBot lightweight WebUI.

只使用 Python 标准库，避免给机器人增加额外依赖。提供：
- config.json / 插件配置的读取与保存
- 运行状态、启动参数、环境信息
- stdout/stderr 实时日志缓冲与最近日志文件读取
"""

from __future__ import annotations

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
import zipfile
import re
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
_webui_reconfigure_lock = threading.RLock()
_update_cache_lock = threading.RLock()
_update_cache = {"timestamp": 0.0, "data": None}
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
GITHUB_REPO = "Qzy327422/XcBot"


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
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + f".{datetime.now().strftime('%Y%m%d%H%M%S')}.bak")
    if path.exists():
        backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.write("\n")


def cleanup_legacy_config_files():
    """删除历史遗留的外部配置文件，强制统一只保留 config.json。"""
    for legacy_path in LEGACY_CONFIG_PATHS:
        try:
            if legacy_path.exists():
                legacy_path.unlink()
        except Exception:
            pass


def _normalize_webui_llm_endpoints(value):
    if not isinstance(value, list):
        return []
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
            "base_url": base_url,
            "model": model,
            "keys": keys,
            "supports_multimodal": bool(raw.get("supports_multimodal", False)),
        })
    return result


def force_apply_llm_endpoints_from_config(cfg: Dict[str, Any]):
    """WebUI 保存后直接刷新 key_manager，兜底保证 LLM 接口列表无需重启。"""
    try:
        from key_manager import key_manager
        others = cfg.get("Others", {}) if isinstance(cfg, dict) else {}
        if not isinstance(others, dict):
            others = {}
        endpoints = _normalize_webui_llm_endpoints(others.get("llm_endpoints", []))
        key_manager.set_endpoints(endpoints)
        default_model = str(others.get("api_default_model", "") or "").strip()
        applied = False
        if default_model:
            applied = key_manager.set_default_by_model(default_model)
        if not applied:
            try:
                default_index = int(others.get("api_default_index", 1) or 1)
            except Exception:
                default_index = 1
            if default_index > 0 and key_manager.get_all_keys():
                applied = key_manager.set_default_by_index(default_index)
        if not applied and key_manager.get_all_keys():
            key_manager.set_default_by_index(1)
        print(f"✅ WebUI 已直接热刷新 LLM 接口列表: endpoints={len(endpoints)}, keys={len(key_manager.get_all_keys())}, current={key_manager.get_current_display()}")
    except Exception as e:
        print(f"WebUI 直接热刷新 LLM 接口列表失败: {e}")


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
        "enabled": bool(webui.get("enabled", True)),
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


def collect_config_bundle() -> Dict[str, Any]:
    cfg = read_json(CONFIG_PATH, {})
    features = dict(DEFAULT_FEATURE_SWITCHES)
    raw_features = cfg.get("FeatureSwitches", {})
    if isinstance(raw_features, dict):
        for key in list(features.keys()):
            if key in raw_features:
                features[key] = bool(raw_features.get(key))
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
                feature_switches[key] = bool(raw.get(key))
    if "feature_switches" in data and isinstance(data["feature_switches"], dict):
        for key in list(feature_switches.keys()):
            if key in data["feature_switches"]:
                feature_switches[key] = bool(data["feature_switches"][key])
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
        {"key": "ai", "title": "AI 配置", "icon": "✨", "desc": "对话接口、超时和大模型接口", "fields": [
            field("Others.api_request_timeout_seconds", "API 超时秒数", "number", "大模型请求超时时间"),
            field("Others.context_max_messages", "上下文最大消息数", "number"),
            field("Others.api_failure_cooldown_seconds", "失败冷却秒数", "number", "单个 API / Key 调用失败后，冷却多久再重试", 5),
            field("Others.api_default_index", "默认 API 编号", "number", "填写聚合后的 API 编号（从 1 开始），留空则按默认模型或首个可用接口"),
            field("Others.api_default_model", "默认模型", "text", "填写后优先锁定到该模型；留空则按默认 API 编号或首个可用接口"),
            field("Others.llm_endpoints", "LLM 接口列表", "endpoints", "配置多个 OpenAI 兼容接口，并为每个接口设置是否支持多模态"),
            field("Others.llm_reply_failover_keywords", "回复切换关键词", "list", "一行一个。若模型回复命中其中任一关键词，则丢弃该回复并按现有失败冷却逻辑自动切换到下一个 API"),
            field("Others.llm_split.enabled", "启用 LLM 分段回复", "bool", "仅对大模型生成结果生效，不影响普通群聊回复是否引用"),
            field("Others.llm_split.mode", "LLM 分段模式", "select", "auto_prompt=大模型自主分段；regex=按正则切分模型输出", "auto_prompt", ["auto_prompt", "regex"]),
            field("Others.llm_split.prompt_suffix", "自主分段提示词", "textarea", "模式一使用。会自动追加到每次 LLM 用户消息后。建议保留 <split> 分隔符说明"),
            field("Others.llm_split.split_regex", "分段正则表达式", "textarea", "模式二使用。用于识别分段点。建议：.*?[。？！~]+|.+$"),
            field("Others.llm_split.filter_regex", "内容过滤正则表达式", "textarea", "模式二使用。对每段文本做清理，例如移除换行：\\n|\\r"),
            field("Others.llm_split.max_chars_no_split", "超过多少字不分段", "number", "最终要发送的整条内容超过[ ]字时，忽略 <split>/正则分段，改为单条发送；填 0 表示不限制", 0),
        ]},
        {"key": "persona", "title": "人格设定", "icon": "💗", "desc": "编辑人设", "fields": [
            field("Others.personality_prompt", "编辑人设", "textarea", "可使用 {bot_name} 与 {user_name} 占位符"),
            field("Others.sensitive_words", "屏蔽词列表", "list", "一行一个，格式：原词=替换词；若只写原词则替换为空。例如：prompt=人格"),
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

    recv_re = re.compile(r"^\[(?P<time>[^\]]+)\] \[[^\]]+\] \[RECV\] (?P<content>.*)$")
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
        recv_match = recv_re.match(line)
        if recv_match:
            ts = _parse_log_timestamp(recv_match.group("time"))
            content = recv_match.group("content")
            total_messages += 1
            if ts is not None:
                message_trend[datetime.fromtimestamp(ts).strftime("%m-%d %H:00")] += 1
            if content.startswith("私聊 "):
                message_scene["私聊"] += 1
            elif content.startswith("群 "):
                message_scene["群聊"] += 1
            else:
                message_scene["其他"] += 1
            continue

        req_match = api_req_re.match(line)
        if req_match:
            ts = _parse_log_timestamp(req_match.group("time")) or 0
            scene = req_match.group("scene").strip()
            model = req_match.group("model").strip()
            host = req_match.group("host").strip()
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
            scene = ok_match.group("scene").strip()
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
            scene = fail_match.group("scene").strip()
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


def _copy_tree_contents(src: Path, dst: Path, skip_names: set[str] | None = None) -> None:
    skip_names = skip_names or set()
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


def _restart_current_process_after_update() -> None:
    """自动更新完成后，拉起新进程并退出旧进程。"""
    argv = [sys.executable] + list(sys.argv)
    try:
        subprocess.Popen(argv, cwd=str(BASE_DIR), close_fds=True)
    except Exception as e:
        raise RuntimeError(f"启动新进程失败: {e}") from e

    def _exit_later():
        try:
            time.sleep(0.8)
        finally:
            os._exit(0)

    threading.Thread(target=_exit_later, name="XcBot-ExitAfterUpdate", daemon=True).start()


def install_latest_update() -> None:
    if not _update_install_lock.acquire(blocking=False):
        raise RuntimeError("已有更新任务正在执行")

    def _worker():
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
                if zip_url:
                    download_candidates.append(zip_url)
                if tag_name:
                    download_candidates.append(f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{urllib.parse.quote(tag_name)}.zip")
                if not download_candidates:
                    raise RuntimeError("未获取到可下载的更新地址")

                last_error = None
                for candidate in download_candidates:
                    try:
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
                        break
                    except Exception as download_error:
                        last_error = download_error
                if last_error is not None:
                    raise last_error

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

                _set_update_install_status("installing", "正在安装更新", latest_version)
                _copy_tree_contents(
                    release_root,
                    BASE_DIR,
                    skip_names={".git", ".github", "config_backup", "data", "temps", "Tools", "__pycache__", "my_bot.lock"},
                )

                if had_old_config and old_config_copy.exists() and CONFIG_PATH.exists():
                    _set_update_install_status("migrating", "正在迁移配置", latest_version)
                    from config_migrate import migrate as migrate_config
                    # 将“更新前当前目录下的老 config.json”迁移合并到“新版本覆盖后的 config.json”。
                    migrate_config(
                        str(old_config_copy),
                        str(CONFIG_PATH),
                        str(BASE_DIR / "config_backup"),
                        remove_old=True,
                    )
                else:
                    print("[更新] 未找到旧版 config.json，已直接使用新版本自带 config.json。")

                _set_update_install_status("dependencies", "正在安装依赖", latest_version)
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(BASE_DIR / "requirements.txt"), "--disable-pip-version-check"],
                    cwd=str(BASE_DIR),
                    check=True,
                )

            _set_update_install_status("restarting", "安装完成，正在重启", latest_version)
            with _update_cache_lock:
                _update_cache["timestamp"] = 0.0
                _update_cache["data"] = None
            _restart_current_process_after_update()
        except Exception as e:
            _set_update_install_status("error", "更新失败", str(e))
            print(f"自动更新失败: {e}")
            traceback.print_exc()
        finally:
            _update_install_lock.release()

    threading.Thread(target=_worker, name="XcBot-AutoUpdate", daemon=True).start()


def fetch_update_info(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    with _update_cache_lock:
        cached = _update_cache.get("data")
        if not force and cached and (now - float(_update_cache.get("timestamp") or 0)) < 300:
            return dict(cached)

    cfg = read_json(CONFIG_PATH, {})
    current_version = str((cfg.get("Others") or {}).get("version_name", "") or "").strip()
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    html_url = f"https://github.com/{GITHUB_REPO}/releases/latest"
    result = {
        "repo": GITHUB_REPO,
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
    }
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": "XcBot-WebUI/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        tag_name = str(payload.get("tag_name") or "").strip()
        latest_version = str(tag_name or payload.get("name") or "").strip()
        release_name = str(payload.get("name") or latest_version or "").strip()
        published_at = str(payload.get("published_at") or payload.get("created_at") or "").strip()
        body = str(payload.get("body") or "").strip()
        release_url = str(payload.get("html_url") or html_url).strip()
        zipball_url = str(payload.get("zipball_url") or "").strip()
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
    except urllib.error.HTTPError as e:
        result.update({"status": "error", "message": f"获取更新失败：HTTP {e.code}"})
    except Exception as e:
        result.update({"status": "error", "message": f"获取更新失败：{e}"})

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
        "webui": get_webui_config(),
        "connection": {
            "protocol": cfg.get("protocol", "OneBot"),
            "mode": connection_cfg.get("mode", ""),
            "host": connection_cfg.get("host", ""),
            "port": connection_cfg.get("port", ""),
            "listener_host": connection_cfg.get("listener_host", ""),
            "listener_port": connection_cfg.get("listener_port", ""),
        },
        "update": fetch_update_info(),
        "update_install": dict(_update_install_status),
        "connection_status": dict(_connection_status),
        "feature_switches": collect_config_bundle().get("feature_switches", {}),
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
            _json_response(self, {"ok": False, "error": "未授权：请在请求头 X-WebUI-Token 或 URL token 参数中提供 access_token"}, 401)
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                _text_response(self, INDEX_HTML)
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
                            merged[key] = bool(raw.get(key))
                if isinstance(feature_switches, dict):
                    for key in merged.keys():
                        if key in feature_switches:
                            merged[key] = bool(feature_switches[key])
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
            elif parsed.path == "/api/update/install":
                install_latest_update()
                _json_response(self, {"ok": True, "message": "已开始安装更新", "data": {"install": dict(_update_install_status), "update": fetch_update_info()}})
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


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>XcBot WebUI</title><link rel="icon" href="/assets/icon.jpg">
  <style>
    :root{--bg0:#06151b;--bg1:#0b2b26;--bg2:#12384a;--bg3:#071017;--glass:rgba(255,255,255,.105);--glass2:rgba(255,255,255,.072);--glass3:rgba(255,255,255,.045);--text:#f2fbff;--muted:rgba(224,242,254,.68);--muted2:rgba(224,242,254,.46);--line:rgba(255,255,255,.14);--line2:rgba(255,255,255,.08);--accent:#38d5ff;--accent2:#7cf7c8;--accent3:#a78bfa;--ok:#42e6a4;--bad:#fb7185;--shadow:0 24px 90px rgba(0,0,0,.42);--shadow2:0 12px 42px rgba(56,213,255,.14);--blur:24px;--radius:26px}
    html[data-theme="light"]{--bg0:#f4f8fb;--bg1:#eef7f3;--bg2:#edf6ff;--bg3:#f8fbff;--glass:rgba(255,255,255,.78);--glass2:rgba(255,255,255,.64);--glass3:rgba(255,255,255,.48);--text:#142334;--muted:rgba(44,62,80,.68);--muted2:rgba(44,62,80,.48);--line:rgba(148,163,184,.24);--line2:rgba(148,163,184,.16);--accent:#3b82f6;--accent2:#34d399;--accent3:#8b5cf6;--ok:#059669;--bad:#e11d48;--shadow:0 24px 72px rgba(148,163,184,.18);--shadow2:0 14px 36px rgba(59,130,246,.14)}
    *{box-sizing:border-box}html{min-height:100%;background:var(--bg0)}body{margin:0;min-height:100vh;color:var(--text);font-family:Inter,Segoe UI,Microsoft YaHei,Arial,sans-serif;overflow-x:hidden;background:radial-gradient(circle at 13% 9%,rgba(124,247,200,.24),transparent 27%),radial-gradient(circle at 72% 14%,rgba(56,213,255,.18),transparent 28%),radial-gradient(circle at 84% 78%,rgba(167,139,250,.16),transparent 30%),linear-gradient(145deg,var(--bg0),var(--bg1) 42%,var(--bg2) 74%,var(--bg3));background-attachment:fixed}body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.28;background-image:linear-gradient(rgba(255,255,255,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.055) 1px,transparent 1px);background-size:38px 38px}body:after{content:"";position:fixed;inset:14px;pointer-events:none;border:1px solid rgba(255,255,255,.08);border-radius:30px;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}button,a,input,textarea,select{font:inherit}button,a{color:inherit}.app{display:grid;grid-template-columns:286px 1fr;min-height:100vh;padding:18px;gap:18px;position:relative;z-index:1}.sidebar{position:sticky;top:18px;height:calc(100vh - 36px);padding:18px 14px;border:1px solid var(--line);border-radius:var(--radius);background:linear-gradient(180deg,rgba(255,255,255,.13),rgba(255,255,255,.055));box-shadow:var(--shadow);backdrop-filter:blur(var(--blur)) saturate(145%);-webkit-backdrop-filter:blur(var(--blur)) saturate(145%);overflow:auto}.brand{display:flex;align-items:center;gap:12px;padding:0 10px 18px}.logo{width:44px;height:44px;border-radius:17px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--accent3));font-size:22px;box-shadow:0 14px 34px rgba(56,213,255,.25);overflow:hidden}.logo img{width:100%;height:100%;object-fit:cover;display:block}.brand h1{font-size:17px;margin:0;font-weight:900;letter-spacing:.2px}.brand p{margin:3px 0 0;color:var(--muted);font-size:12px}.nav-title{margin:14px 12px 8px;color:var(--muted2);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.nav{display:flex;flex-direction:column;gap:8px}.nav button{border:1px solid transparent;background:transparent;text-align:left;border-radius:17px;padding:12px 13px;display:flex;align-items:center;gap:11px;cursor:pointer;color:var(--muted);font-weight:750;transition:.2s ease}.nav button:hover{color:var(--text);background:rgba(255,255,255,.075);border-color:var(--line2);transform:translateX(2px)}.nav button.active{color:var(--text);background:linear-gradient(135deg,rgba(56,213,255,.26),rgba(124,247,200,.11));border-color:rgba(56,213,255,.32);box-shadow:inset 3px 0 0 var(--accent),0 12px 28px rgba(56,213,255,.10)}.main{min-width:0;padding:0;border:1px solid var(--line);border-radius:var(--radius);background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.03));box-shadow:var(--shadow);backdrop-filter:blur(16px) saturate(135%);-webkit-backdrop-filter:blur(16px) saturate(135%);overflow:hidden}.topbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:14px;padding:18px 22px;background:linear-gradient(180deg,rgba(6,21,27,.72),rgba(6,21,27,.34));backdrop-filter:blur(22px) saturate(145%);border-bottom:1px solid var(--line2)}html[data-theme="light"] .topbar{background:linear-gradient(180deg,rgba(255,255,255,.70),rgba(255,255,255,.40))}.title h2{margin:0;font-size:24px;font-weight:950;letter-spacing:.2px}.title p{margin:5px 0 0;color:var(--muted);font-size:13px}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.btn{border:1px solid var(--line);background:linear-gradient(180deg,var(--glass),var(--glass2));border-radius:15px;padding:10px 14px;cursor:pointer;text-decoration:none;color:var(--text);font-weight:800;box-shadow:inset 0 1px 0 rgba(255,255,255,.10);transition:.2s ease}.btn:hover{transform:translateY(-1px);border-color:rgba(56,213,255,.36);box-shadow:var(--shadow2)}.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent3));border-color:transparent;color:#031018;box-shadow:0 16px 36px rgba(56,213,255,.24)}.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:linear-gradient(180deg,var(--glass),var(--glass3));border-radius:999px;padding:7px 11px;color:var(--muted);font-size:12px;font-weight:800}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:18px;padding:22px}.card{grid-column:span 12;position:relative;background:linear-gradient(145deg,rgba(255,255,255,.12),rgba(255,255,255,.055));border:1px solid var(--line);border-radius:var(--radius);padding:22px;box-shadow:0 20px 60px rgba(0,0,0,.18),inset 0 1px 0 rgba(255,255,255,.12);backdrop-filter:blur(var(--blur)) saturate(150%);-webkit-backdrop-filter:blur(var(--blur)) saturate(150%);overflow:hidden}.card:before{content:"";position:absolute;inset:-1px;border-radius:inherit;pointer-events:none;background:radial-gradient(circle at 18% 0%,rgba(124,247,200,.18),transparent 34%),radial-gradient(circle at 88% 8%,rgba(56,213,255,.16),transparent 35%)}.card>*{position:relative}.half{grid-column:span 6}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:16px}.section-head h3{margin:0;font-size:18px;font-weight:930}.section-head p{margin:5px 0 0;color:var(--muted);font-size:13px}.form-grid,.feature-grid,.mini-stats{display:grid;gap:15px}.form-grid{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}.feature-grid{grid-template-columns:repeat(auto-fit,minmax(255px,1fr))}.mini-stats{grid-template-columns:repeat(auto-fit,minmax(155px,1fr))}.field,.feature,.stat{border:1px solid var(--line2);background:linear-gradient(180deg,rgba(255,255,255,.075),rgba(255,255,255,.035));border-radius:21px;padding:15px;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}.feature{transition:.2s ease}.feature:hover{transform:translateY(-2px);border-color:rgba(56,213,255,.26);box-shadow:0 16px 36px rgba(0,0,0,.14)}.label{display:flex;justify-content:space-between;gap:10px;margin-bottom:9px;font-weight:850}.desc{color:var(--muted);font-size:12px;margin-top:9px;line-height:1.5}input,textarea,select{width:100%;border:1px solid var(--line);border-radius:16px;background:rgba(5,12,25,.34);color:var(--text);padding:11px 13px;outline:none;box-shadow:inset 0 1px 0 rgba(255,255,255,.08);transition:.18s ease}html[data-theme="light"] input,html[data-theme="light"] textarea,html[data-theme="light"] select{background:rgba(255,255,255,.55)}input:focus,textarea:focus,select:focus{border-color:rgba(56,213,255,.55);box-shadow:0 0 0 4px rgba(56,213,255,.12),inset 0 1px 0 rgba(255,255,255,.10)}textarea{min-height:132px;resize:vertical;font-family:Consolas,JetBrains Mono,monospace}.json-area{min-height:420px}.switch{position:relative;width:58px;height:32px;flex:0 0 auto;border-radius:999px;background:rgba(100,116,139,.35);border:1px solid var(--line);cursor:pointer;box-shadow:inset 0 1px 3px rgba(0,0,0,.25)}.switch:after{content:"";position:absolute;top:4px;left:4px;width:22px;height:22px;border-radius:50%;background:#dbeafe;transition:.22s cubic-bezier(.2,.8,.2,1);box-shadow:0 5px 14px rgba(0,0,0,.25)}.switch.on{background:linear-gradient(135deg,var(--accent),var(--accent2))}.switch.on:after{left:30px;background:#fff}.feature-foot,.kv{display:grid;gap:9px 12px}.feature-foot{grid-template-columns:1fr auto;align-items:center}.kv{grid-template-columns:150px 1fr;font-size:13px}.kv div:nth-child(odd),.feature p,.stat span{color:var(--muted)}.stat b{display:block;font-size:24px;font-weight:950;background:linear-gradient(135deg,var(--text),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}pre.log{margin:0;white-space:pre-wrap;word-break:break-word;max-height:560px;overflow:auto;font-family:Consolas,JetBrains Mono,monospace;font-size:12px;line-height:1.55;background:rgba(0,0,0,.22);border:1px solid var(--line);border-radius:20px;padding:16px}pre.log.compact{max-height:320px;padding:8px 16px;line-height:1.28}.toast{position:fixed;right:24px;bottom:24px;max-width:440px;border:1px solid var(--line);background:linear-gradient(180deg,var(--glass),var(--glass2));backdrop-filter:blur(22px);border-radius:18px;padding:13px 15px;display:none;box-shadow:var(--shadow);z-index:20}.toast.show{display:block}.ok{color:var(--ok)}.bad{color:var(--bad)}.file-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}@media(max-width:980px){.app{grid-template-columns:1fr;padding:12px}.sidebar{position:relative;top:auto;height:auto}.main{min-height:70vh}.topbar{padding:14px}.half{grid-column:span 12}.kv{grid-template-columns:1fr}.grid{padding:14px}.card{padding:16px}}
  </style>
</head>
<body><div class="app"><aside class="sidebar"><div class="brand"><div class="logo"><img src="/assets/icon.jpg" alt="XcBot"></div><div><h1 id="brandName">XcBot</h1><p>实时 Web 管理台</p></div></div><div class="nav-title">功能列表</div><nav id="nav" class="nav"></nav><div class="nav-title">OneBot / Hyper 连接状态</div><div id="connectionStatus" class="pill">加载中...</div><div id="connectionDetail" class="desc" style="margin:10px 12px 0 12px"></div></aside><main class="main"><div class="topbar"><div class="title"><h2 id="pageTitle">加载中...</h2><p id="pageDesc">正在连接 WebUI</p></div><div class="toolbar"><span id="saveState" class="pill">未加载</span><button class="btn" onclick="refreshAll(true)">立即同步</button><button class="btn" id="themeBtn" onclick="toggleTheme()">🌙 深色</button><button class="btn primary" onclick="saveAll()">保存设置</button></div></div><section id="content" class="grid"></section></main></div><div id="toast" class="toast"></div>
<script>
let state={bundle:null,current:localStorage.webuiPage||'welcome',dirty:false,saving:false,lastInputAt:0,expectedReloadAfterUpdate:false,apiFailCount:0,reloadTimer:null};
const featureFieldMap={ai_chat:['Others.llm_split.enabled','Others.llm_split.mode','Others.llm_split.prompt_suffix','Others.llm_split.split_regex','Others.llm_split.filter_regex','Others.llm_split.max_chars_no_split'],group_chat:['Others.group_random_reply_probability','Others.group_random_reply_quote'],emoji_plus_one:['Others.emoji_plus_one_cooldown_seconds'],poke_reply:['Others.poke_cooldown_seconds'],split_reply_quote:[],weak_blacklist:['Others.weak_blacklist_trigger_probability','Others.weak_blacklist_users'],summary:['Others.summary_per_day_limit','Others.summary_max_messages'],compression_commands:['Others.compression_threshold','Others.compression_keep_recent','Others.auto_compress_after_messages'],plugins_external:[]};
const esc=s=>String(s??'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
const token=()=>new URLSearchParams(location.search).get('token')||localStorage.webuiToken||'';
const el=id=>document.getElementById(id);
const DRAFT_KEY='xcbotWebuiFormDraft';
function loadDraft(){try{return JSON.parse(localStorage.getItem(DRAFT_KEY)||'null')||null}catch(e){return null}}
function saveDraft(){try{if(state.bundle?.form_values)localStorage.setItem(DRAFT_KEY,JSON.stringify(state.bundle.form_values))}catch(e){}}
function clearDraft(){try{localStorage.removeItem(DRAFT_KEY)}catch(e){}}
function applyDraft(bundle){const draft=loadDraft();if(draft&&bundle?.form_values){bundle.form_values=Object.assign({},bundle.form_values,draft);state.dirty=true;state.lastInputAt=Date.now();const save=el('saveState');if(save)save.textContent='有未保存草稿'}}
async function api(path,opt={}){opt.headers=Object.assign({'Content-Type':'application/json','X-WebUI-Token':token()},opt.headers||{});const r=await fetch(path,opt),j=await r.json();if(!j.ok)throw new Error(j.error||'请求失败');return j.data??j}
function toast(msg,ok=true){const t=el('toast'),save=el('saveState');if(t){t.textContent=msg;t.className='toast show '+(ok?'ok':'bad');clearTimeout(t._timer);t._timer=setTimeout(()=>t.classList.remove('show'),2600)}if(save)save.textContent=msg}
const pages=()=>state.bundle?.ui_schema||[];
const meta=()=>pages().find(x=>x.key===state.current)||pages()[0]||{title:'WebUI',desc:''};
function setTheme(t){document.documentElement.dataset.theme=t;localStorage.webuiTheme=t;const btn=el('themeBtn');if(btn)btn.textContent=t==='light'?'☀️ 浅色':'🌙 深色'}
function toggleTheme(){setTheme((document.documentElement.dataset.theme||'dark')==='dark'?'light':'dark')}
function gotoPage(k){if(!pages().some(p=>p.key===k))k='welcome';state.current=k;localStorage.webuiPage=k;render()}
function renderNav(){const nav=el('nav');if(nav)nav.innerHTML=pages().map(p=>`<button class="${p.key===state.current?'active':''}" onclick="gotoPage('${p.key}')"><span>${p.icon||'•'}</span><span>${esc(p.title)}</span></button>`).join('')}
function renderConnectionStatus(){const s=state.bundle?.status||{},cs=s.connection_status||{},cfg=s.connection||{};const statusEl=el('connectionStatus'),detailEl=el('connectionDetail');if(statusEl){const text=cs.text||'未知状态';statusEl.textContent=text;statusEl.className='pill '+((cs.state==='connected')?'ok':(cs.state==='failed'||cs.state==='disconnected'||cs.state==='stopped')?'bad':'')}if(detailEl){const lines=[];if(cs.detail)lines.push(cs.detail);const endpoint=[cfg.protocol,cfg.host&&cfg.port?`${cfg.host}:${cfg.port}`:''].filter(Boolean).join(' · ');if(endpoint)lines.push(endpoint);detailEl.textContent=lines.join(' | ')||'暂无连接详情'}}
function render(){if(!state.bundle)return;const logScroll=captureLogScrollState();renderNav();renderConnectionStatus();const m=meta(),titleEl=el('pageTitle'),descEl=el('pageDesc'),brandEl=el('brandName'),contentEl=el('content');if(titleEl)titleEl.textContent=(m.icon?m.icon+' ':'')+m.title;if(descEl){const desc=Object.prototype.hasOwnProperty.call(m,'desc')?m.desc:'所有数值均可在此直接修改并保存';descEl.textContent=desc;descEl.style.display=desc?'':'none'}if(brandEl)brandEl.textContent=state.bundle.status?.project||'XcBot';if(contentEl)contentEl.innerHTML=state.current==='welcome'?renderWelcome():state.current==='stats'?renderStats():state.current==='features'?renderFeatures():state.current==='logs'?renderLogs():renderForm(m);if(state.current==='welcome'||state.current==='logs')scheduleLogScrollAfterRender(logScroll)}
function renderWelcome(){const s=state.bundle.status||{},u=s.update||{},ui=s.update_install||{},cs=s.connection_status||{},cc=s.connection||{},updateBusy=['downloading','extracting','installing','migrating','dependencies','restarting'].includes(ui.state),updatePillClass=u.status==='latest'?'ok':(u.status==='outdated'||u.status==='error'||ui.state==='error')?'bad':'';return `<div class="card"><div class="section-head"><div><h3>运行概览</h3></div><span class="pill ${cs.state==='connected'?'ok':(cs.state==='failed'||cs.state==='disconnected'||cs.state==='stopped')?'bad':''}">${esc(cs.text||'未知状态')}</span></div><div class="mini-stats"><div class="stat"><b>${Math.floor((s.uptime_seconds||0)/60)}</b><span>已运行分钟</span></div><div class="stat"><b>${esc(s.pid)}</b><span>进程 PID</span></div><div class="field" style="grid-column:span 2;min-height:auto;display:flex;flex-direction:column;justify-content:center"><div class="label" style="margin-bottom:6px"><span>获取更新</span><span class="pill ${updatePillClass}">${esc(ui.state&&ui.state!=='idle'?ui.text:(u.message||'暂未检查更新'))}</span></div><div class="desc" style="margin-top:0">当前 ${esc(s.version||'--')} / 最新 ${esc(u.latest_version||'--')}</div><div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px"><button class="btn" ${updateBusy?'disabled':''} onclick="checkUpdate()">检查更新</button><button class="btn primary" ${(updateBusy||!u.has_update)?'disabled':''} onclick="installUpdate()">安装更新</button></div><div class="desc">${esc(ui.detail||u.detail||u.release_name||'安装更新会自动下载、覆盖程序、迁移配置并重启。安装后需手动刷新页面。')}</div></div></div></div><div class="card half"><div class="section-head"><div><h3>详细信息</h3><p>环境与启动参数</p></div></div><div class="kv">${[['版本号',(s.project||'')+' '+(s.version||'')],['机器人名',s.bot_name],['运行目录',s.cwd],['Python',s.python],['平台',s.platform],['启动参数',JSON.stringify(s.argv||[])]].map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1])}</div>`).join('')}</div></div><div class="card half"><div class="section-head"><div><h3>连接状态</h3><p>OneBot / Hyper 实时状态</p></div></div><div class="kv">${[['当前状态',cs.text||'未知状态'],['状态详情',cs.detail||'暂无'],['协议',cc.protocol||''],['连接地址',(cc.host&&cc.port)?`${cc.host}:${cc.port}`:''],['监听地址',(cc.listener_host&&cc.listener_port)?`${cc.listener_host}:${cc.listener_port}`:''],['连接模式',cc.mode||'']].map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1])}</div>`).join('')}</div></div><div class="card"><div class="section-head"><div><h3>最近日志</h3></div><div style="display:flex;gap:8px;flex-wrap:wrap">${renderAutoScrollToggle()}<button class="btn" onclick="gotoPage('logs')">查看全部</button></div></div>${renderLogPre(160,'recent')}</div>`}
function chartBars(items,max=0,unit='',color='linear-gradient(135deg,var(--accent),var(--accent2))'){const arr=Array.isArray(items)?items:[];const peak=max||Math.max(1,...arr.map(x=>Number(x.value)||0));return `<div style="display:grid;gap:10px">${arr.length?arr.map(x=>`<div><div style="display:flex;justify-content:space-between;gap:12px;margin-bottom:6px"><span style="font-weight:800">${esc(x.label||x.model||x.session||'-')}</span><span class="pill">${esc((x.value??x.tokens??0)+unit)}</span></div><div style="height:12px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden"><div style="height:100%;width:${Math.max(4,Math.round(((Number(x.value??x.tokens)||0)/peak)*100))}%;background:${color};border-radius:999px"></div></div></div>`).join(''):`<div class="desc">暂无数据</div>`}</div>`}
function donutSegments(items){const arr=(Array.isArray(items)?items:[]).filter(x=>Number(x.value)>0);if(!arr.length)return '<div class="desc">暂无数据</div>';const total=arr.reduce((s,x)=>s+(Number(x.value)||0),0)||1;let acc=0;const colors=['#38d5ff','#7cf7c8','#a78bfa','#fb7185','#f59e0b','#60a5fa'];const stops=[];arr.forEach((x,i)=>{const start=Math.round(acc/total*360);acc+=Number(x.value)||0;const end=Math.round(acc/total*360);stops.push(`${colors[i%colors.length]} ${start}deg ${end}deg`)});return `<div style="display:grid;grid-template-columns:180px 1fr;gap:18px;align-items:center"><div style="width:180px;height:180px;margin:auto;border-radius:50%;background:conic-gradient(${stops.join(',')});position:relative;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)"><div style="position:absolute;inset:24px;border-radius:50%;background:rgba(6,21,27,.88);display:grid;place-items:center;text-align:center;font-weight:900">${total}<br><span style="font-size:12px;color:var(--muted)">消息总量</span></div></div><div style="display:grid;gap:10px">${arr.map((x,i)=>`<div style="display:flex;justify-content:space-between;gap:12px;align-items:center"><div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${colors[i%colors.length]}"></span><span>${esc(x.label)}</span></div><span class="pill">${esc(x.value)}</span></div>`).join('')}</div></div>`}
function renderStats(){const st=state.bundle.statistics||{},sum=st.summary||{},models=st.model_ranking||[],top10=st.session_tokens_top10_1d||[],apiHistory=st.api_history||[];return `<div class="card"><div class="section-head"><div><h3>核心指标</h3><p>察看运行质量与模型负载</p></div><button class="btn" onclick="refreshAll(true)">刷新统计</button></div><div class="mini-stats"><div class="stat"><b>${esc(sum.message_count||0)}</b><span>消息数</span></div><div class="stat"><b>${esc(sum.api_calls||0)}</b><span>模型调用次数</span></div><div class="stat"><b>${esc(sum.api_tokens||0)}</b><span>累计 Tokens</span></div><div class="stat"><b>${esc(sum.model_count||0)}</b><span>参与模型数</span></div></div></div><div class="card half"><div class="section-head"><div><h3>消息来源占比</h3><p>私聊 / 群聊分布</p></div></div>${donutSegments(st.message_scene||[])}</div><div class="card half"><div class="section-head"><div><h3>消息趋势</h3><p>最近 24 个统计时段</p></div></div>${chartBars(st.message_trend||[],0,' 条','linear-gradient(135deg,var(--accent),var(--accent3))')}</div><div class="card half"><div class="section-head"><div><h3>模型调用排名及用量</h3><p>按 Tokens 与调用次数综合排序</p></div></div>${chartBars(models.slice(0,8).map(x=>({label:`${x.model} · ${x.calls}次`,value:x.tokens})),0,' Tok','linear-gradient(135deg,var(--accent2),var(--accent3))')}</div><div class="card half"><div class="section-head"><div><h3>最近 1 天会话 Tokens Top 10</h3><p>按会话维度统计 Token 消耗</p></div></div>${chartBars(top10.map(x=>({label:`${x.session} · ${x.calls}次`,value:x.tokens})),0,' Tok','linear-gradient(135deg,#fb7185,#f59e0b)')}</div><div class="card"><div class="section-head"><div><h3>模型调用历史</h3><p>最近 30 条调用记录</p></div></div>${apiHistory.length?`<div style="display:grid;gap:10px">${apiHistory.map(x=>`<div class="field" style="padding:14px"><div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap"><div><div style="font-weight:900">${esc(x.model)} <span class="pill ${x.status==='success'?'ok':x.status==='failed'?'bad':''}">${esc(x.status)}</span></div><div class="desc">${esc(x.scene)} · ${esc(x.host)} · ${esc(x.time)}</div></div><div style="display:flex;gap:8px;flex-wrap:wrap"><span class="pill">${esc(x.message_count||0)} 条消息</span><span class="pill">${esc(x.tokens||0)} Tok</span></div></div><div class="desc" style="margin-top:10px">${esc(x.preview||'')||'（无预览）'}</div></div>`).join('')}</div>`:`<div class="desc">暂无模型调用历史。先让机器人运行一段时间后再来看，会更完整。</div>`}</div><div class="card"><div class="section-head"><div><h3>统计说明</h3><p>当前版本按日志推导统计数据</p></div></div><div class="kv">${[['消息数','来自 [RECV] 日志'],['模型调用历史','来自 [API] 请求/成功/失败日志'],['模型排名及用量','按模型累计调用次数、成功数、失败数与 Tokens'],['最近 1 天会话 Top10','按 scene 聚合最近 24 小时 Tokens'],['数据更新时间',new Date((st.generated_at||0)*1000).toLocaleString()]].map(r=>`<div>${esc(r[0])}</div><div>${esc(r[1])}</div>`).join('')}</div></div>`}
function renderForm(m){const v=state.bundle.form_values||{};return `<div class="card"><div class="section-head"><div><h3>${esc(m.title)}</h3><p>${esc(m.desc)}</p></div></div><div class="form-grid">${(m.fields||[]).map(f=>renderField(f,v[f.path])).join('')}</div></div>`}
function renderField(f,v,compact=false){const id='f_'+f.path.replace(/[^a-zA-Z0-9]/g,'_');let input='';if(f.type==='bool')input=`<div class="switch ${v?'on':''}" onclick="setBool('${f.path}',this)"></div>`;else if(f.type==='select')input=`<select id="${id}" onchange="setValue('${f.path}',this.value)">${(f.options||[]).map(o=>`<option value="${esc(o)}" ${String(o)===String(v)?'selected':''}>${esc(o)}</option>`).join('')}</select>`;else if(f.type==='list')input=`<textarea ${compact?'style="min-height:84px"':''} id="${id}" oninput="setValue('${f.path}',this.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))">${esc(Array.isArray(v)?v.join('\n'):(v??''))}</textarea>`;else if(f.type==='textarea')input=`<textarea ${compact?'style="min-height:84px"':'style="min-height:280px"'} id="${id}" oninput="setValue('${f.path}',this.value)">${esc(v??'')}</textarea>`;else if(f.type==='endpoints')input=renderEndpointsEditor(v||[]);else input=`<input id="${id}" type="${f.type==='password'?'password':f.type==='number'?'number':'text'}" step="any" value="${esc(v??'')}" oninput="setValue('${f.path}',${f.type==='number'?'num(this.value)':'this.value'})">`;return `<div class="field"><div class="label"><span>${esc(f.label)}</span>${f.type==='bool'?input:''}</div>${f.type==='bool'?'':input}<div class="desc">${esc(f.desc||f.path)}</div></div>`}
const num=v=>{const n=Number(v);return Number.isFinite(n)?n:v};
function setValue(path,value){state.bundle.form_values[path]=value;if(path==='manage_users'){state.bundle.manage_users=Array.isArray(value)?value:[];state.bundle.super_users=Array.isArray(value)?value:[]}if(path==='black_list'){state.bundle.blacklist_file=Array.isArray(value)?value:[]}state.dirty=true;state.lastInputAt=Date.now();saveDraft();const save=el('saveState');if(save)save.textContent='有未保存更改'}
function syncCurrentPageFieldsFromDom(){if(!state.bundle)return;const page=meta();const values=state.bundle.form_values||{};(page.fields||[]).forEach(f=>{const id='f_'+f.path.replace(/[^a-zA-Z0-9]/g,'_');if(f.type==='bool'){return}if(f.type==='endpoints'){const arr=Array.isArray(values[f.path])?[...values[f.path]]:[];values[f.path]=arr.map((ep,i)=>{const base=document.getElementById('ep_base_'+i),model=document.getElementById('ep_model_'+i),keys=document.getElementById('ep_keys_'+i),mm=document.getElementById('ep_mm_'+i);return {base_url:base?base.value:(ep.base_url||''),model:model?model.value:(ep.model||''),supports_multimodal:mm?!!mm.checked:!!ep.supports_multimodal,keys:(keys?keys.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean):(Array.isArray(ep.keys)?ep.keys:[]))}});return}const el=document.getElementById(id);if(!el)return;if(f.type==='list')values[f.path]=el.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);else if(f.type==='textarea'||f.type==='text'||f.type==='password'||f.type==='select')values[f.path]=el.value;else if(f.type==='number')values[f.path]=num(el.value)});state.bundle.form_values=values;const manageUsers=values.manage_users;if(Array.isArray(manageUsers)){state.bundle.manage_users=manageUsers;state.bundle.super_users=manageUsers}const blackList=values.black_list;if(Array.isArray(blackList)){state.bundle.blacklist_file=blackList}}
function setJsonValue(path,txt){try{setValue(path,JSON.parse(txt||'null'))}catch(e){state.dirty=true;const save=el('saveState');if(save)save.textContent='JSON 暂未通过校验'}}
function setBool(path,el){const v=!el.classList.contains('on');el.classList.toggle('on',v);setValue(path,v)}
function shouldAutoRefreshPage(){return state.current==='welcome'||state.current==='logs'||state.current==='stats'}
function renderFeatures(){const map=state.bundle.feature_switches||{},groups={};const fields=(state.bundle.ui_schema.find(x=>x.key==='features')||{}).fields||[];const fieldMap=Object.fromEntries(fields.map(f=>[f.path,f]));(state.bundle.feature_meta||[]).forEach(x=>(groups[x.group]||(groups[x.group]=[])).push(x));return Object.keys(groups).map(g=>`<div class="card"><div class="section-head"><div><h3>${esc(g)}</h3><p>功能配置</p></div><span class="pill">${groups[g].length} 项</span></div><div class="feature-grid">${groups[g].map(it=>{const rel=(featureFieldMap[it.key]||[]).map(p=>fieldMap[p]).filter(Boolean);return `<div class="feature"><h4>${esc(it.title)}</h4><p>${esc(it.desc)}</p><div class="feature-foot"><span class="pill">${esc(it.key)}</span><div class="switch ${map[it.key]?'on':''}" onclick="toggleFeature('${it.key}',this)"></div></div>${rel.length?`<div style="margin-top:12px;display:grid;gap:10px">${rel.map(f=>renderField(f,state.bundle.form_values[f.path],true)).join('')}</div>`:''}</div>`}).join('')}</div></div>`).join('')}
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
function renderEndpointsEditor(list){const rows=(Array.isArray(list)?list:[]).map((ep,i)=>`<div class="field" style="padding:12px"><div class="label"><span>接口 #${i+1}</span><button class="btn" type="button" onclick="removeEndpoint(${i})">删除</button></div><input id="ep_base_${i}" placeholder="base_url" value="${esc(ep.base_url||'')}" oninput="updateEndpoint(${i},'base_url',this.value)"><div style="height:8px"></div><input id="ep_model_${i}" placeholder="model" value="${esc(ep.model||'')}" oninput="updateEndpoint(${i},'model',this.value)"><div style="height:8px"></div><label style="display:flex;align-items:center;gap:10px;margin:6px 0 10px 0"><input id="ep_mm_${i}" type="checkbox" ${ep.supports_multimodal?'checked':''} onchange="updateEndpoint(${i},'supports_multimodal',this.checked)"><span>支持多模态</span></label><textarea id="ep_keys_${i}" style="min-height:84px" placeholder="keys，一行一个" oninput="updateEndpoint(${i},'keys',this.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))">${esc(Array.isArray(ep.keys)?ep.keys.join('\n'):'')}</textarea></div>`).join('');return `<div style="display:grid;gap:10px">${rows||'<div class="desc">暂无接口，点击下方按钮新增</div>'}<button class="btn" type="button" onclick="addEndpoint()">新增大模型接口</button></div>`}
function updateEndpoint(index,key,value){const arr=Array.isArray(state.bundle.form_values['Others.llm_endpoints'])?[...state.bundle.form_values['Others.llm_endpoints']]:[];arr[index]=Object.assign({base_url:'',model:'',keys:[],supports_multimodal:false},arr[index]||{});arr[index][key]=value;setValue('Others.llm_endpoints',arr)}
function addEndpoint(){const arr=Array.isArray(state.bundle.form_values['Others.llm_endpoints'])?[...state.bundle.form_values['Others.llm_endpoints']]:[];arr.push({base_url:'',model:'',keys:[],supports_multimodal:false});setValue('Others.llm_endpoints',arr);render()}
function removeEndpoint(index){const arr=Array.isArray(state.bundle.form_values['Others.llm_endpoints'])?[...state.bundle.form_values['Others.llm_endpoints']]:[];arr.splice(index,1);setValue('Others.llm_endpoints',arr);render()}
async function saveAll(silent=false){if(!state.bundle||state.saving)return;state.saving=true;const save=el('saveState');if(save)save.textContent='正在保存...';try{syncCurrentPageFieldsFromDom();saveDraft();const manageUsers=state.bundle.form_values?.manage_users??state.bundle.manage_users;const blackList=state.bundle.form_values?.black_list??state.bundle.blacklist_file;const saved=await api('/api/ui-state',{method:'POST',body:JSON.stringify({form_values:state.bundle.form_values||{},feature_switches:state.bundle.feature_switches||{},super_users:manageUsers,manage_users:manageUsers,blacklist_file:blackList})});clearDraft();state.bundle=saved;state.dirty=false;render();await refreshAll(true);if(!silent)toast('设置已保存并已从 config 重新同步',true)}catch(e){toast(e.message,false)}finally{state.saving=false}}
function isEditingField(){const el=document.activeElement;return !!(el&&['INPUT','TEXTAREA','SELECT'].includes(el.tagName))}
function shouldPauseAutoRefresh(){return !!(state.dirty||isEditingField()||(Date.now()-(state.lastInputAt||0)<15000))}
async function refreshAll(force=false){if(!force&&!shouldAutoRefreshPage())return;if(shouldPauseAutoRefresh()&&!force)return;try{const data=await api('/api/ui-state');state.apiFailCount=0;if(shouldPauseAutoRefresh()&&!force)return;state.bundle=data;applyDraft(state.bundle);const installState=data?.status?.update_install?.state||'';if(['restarting','idle'].includes(installState)&&state.expectedReloadAfterUpdate){scheduleReloadAfterUpdate()}render();const save=el('saveState');if(save&&!state.dirty)save.textContent='已同步 '+new Date().toLocaleTimeString()}catch(e){state.apiFailCount=(state.apiFailCount||0)+1;if(state.expectedReloadAfterUpdate&&state.apiFailCount>=2)scheduleReloadAfterUpdate();if(!state.expectedReloadAfterUpdate)toast(e.message,false)}}
setTheme(localStorage.webuiTheme||'dark');refreshAll(true);setInterval(()=>refreshAll(false),3000);setInterval(()=>{if(state.current==='welcome'||state.current==='logs')scrollLogsToBottom()},500);
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