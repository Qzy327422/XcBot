# -*- coding: utf-8 -*-
try:
    import faulthandler
    faulthandler.enable()
except Exception as e:
    print(f"faulthandler 初始化失败: {e}，但不影响主要功能")

# ==================== 基础导入 ====================
import asyncio
import aiohttp
import base64
import datetime
import ipaddress
import os
import random
import re
import urllib.parse
import mimetypes
import uuid
import emoji
import time
import traceback
import json
import pickle
import threading
import platform
import psutil
import logging
# ponytail: GPUtil 只在 get_system_resource_info 里用一次，改为懒加载
from typing import Set, Dict, Optional
from collections import defaultdict, deque, Counter, OrderedDict
from openai import OpenAI
from datetime import date
import atexit
import signal
import sys
from pathlib import Path
import importlib

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ==================== stdout 时间戳包装 ====================
# 仅在重定向到文件时生效（isatty 为 False），交互终端保持原样。
class _TimestampedWriter:
    """给每行 print 输出加上 [HH:MM:SS] 前缀，方便日志追溯。"""
    def __init__(self, wrapped):
        self._w = wrapped
    def write(self, s):
        if not s or s == "\n":
            self._w.write(s)
            return
        import time as _t
        ts = _t.strftime("[%H:%M:%S] ")
        lines = s.split("\n")
        out = []
        for i, line in enumerate(lines):
            if i == len(lines) - 1 and line == "":
                out.append("")
            elif line:
                out.append(ts + line)
            else:
                out.append("")
        self._w.write("\n".join(out))
    def flush(self):
        self._w.flush()
    def __getattr__(self, name):
        return getattr(self._w, name)

try:
    if not sys.stdout.isatty():
        sys.stdout = _TimestampedWriter(sys.stdout)
    if not sys.stderr.isatty():
        sys.stderr = _TimestampedWriter(sys.stderr)
except Exception:
    pass

# ==================== 配置全局异常处理器 ====================
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 配置日志系统
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "error.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("XcBot")


def global_exception_handler(exc_type, exc_value, exc_traceback):
    """全局未捕获异常处理器 - 记录到日志并优雅降级"""
    if issubclass(exc_type, KeyboardInterrupt):
        # 用户主动中断，不记录
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    logger.critical(
        "未捕获的异常",
        exc_info=(exc_type, exc_value, exc_traceback)
    )
    print(f"\n❌ 程序遇到严重错误，详情已记录到 {LOG_DIR / 'error.log'}")
    print(f"错误类型: {exc_type.__name__}")
    print(f"错误信息: {exc_value}")


sys.excepthook = global_exception_handler

# ==================== 先初始化配置，但不破坏NapCat连接 ====================
from Hyper import Configurator

# 初始化配置管理器
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = str(BASE_DIR / "config.json")
try:
    from config_migrate import ensure_config_up_to_date
    ensure_config_up_to_date(CONFIG_FILE)
except Exception as e:
    print(f"配置自动升级失败，将继续按当前 config.json 启动: {e}")
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file=CONFIG_FILE).load_from_file())
config = Configurator.cm.get_cfg()

# ==================== 导入 key_manager ====================
from key_manager import key_manager

# ==================== 然后再导入其他 Hyper 模块 ====================
from Hyper import Listener, Events, Logger, Manager, Segments
from Hyper.Utils import Logic
from Hyper.Events import *

# ==================== 修复 Hyper 框架 KeyQueue 忙等待 ====================
# 原实现：while 1: try: return contents[key] except KeyError: pass
# 缺陷1：纯忙等，断线时响应永远不来，线程永久 100% CPU 空转
# 缺陷2：返回后不删除 key，responses 字典只增不减，内存无限增长
# 在 28971 次重连场景下，积累的线程和响应体积庞大
def _patched_keyqueue_get(self, key: str, timeout: float = 60.0):
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if key in self.contents:
            return self.contents.pop(key)  # pop 而非 return，消费后删除
        _time.sleep(0.01)
    raise TimeoutError(f"KeyQueue.get timeout waiting for echo={key}")

Logic.KeyQueue.get = _patched_keyqueue_get

# ==================== 自定义模块导入 ====================
import Quote
from webui import start_webui, stop_webui, DEFAULT_FEATURE_SWITCHES, set_connection_status
from webui_core.features import DEFAULT_GROUP_JOIN_WELCOME_TEXT
try:
    # 自动更新走重启路径时也要先释放目录锁，避免新进程被锁挡在外
    from webui import set_pre_restart_callback as _set_pre_restart_callback
except Exception:
    _set_pre_restart_callback = None
try:
    from webui import set_qq_send_callback as _set_qq_send_callback
except Exception:
    _set_qq_send_callback = None
try:
    from webui import set_debug_self_message_callback as _set_debug_self_message_callback
except Exception:
    _set_debug_self_message_callback = None


def load_user_cfg() -> dict:
    """优先从 config.json 原始内容读取 Others，确保 WebUI 保存后可立即热应用。"""
    runtime_others = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            runtime_cfg = json.load(f)
        if isinstance(runtime_cfg, dict) and isinstance(runtime_cfg.get("Others"), dict):
            runtime_others = runtime_cfg.get("Others") or {}
    except Exception as e:
        print(f"读取 config.json 的 Others 失败，将回退到 Configurator: {e}")

    base_others = getattr(config, "others", {}) or {}
    others = dict(base_others)
    others.update(runtime_others)

    defaults = {
        "project_name": others.get("project_name", "XcBot"),
        "bot_name": others.get("bot_name", "忻城"),
        "bot_name_en": others.get("bot_name_en", "XinCheng"),
        "version_name": others.get("version_name", "2.0"),
        "reminder": others.get("reminder", "/"),
        "slogan": others.get("slogan", "✨ 忻城 ✨"),
        "robot_name_triggers": others.get("robot_name_triggers", [others.get("bot_name", "忻城")]),
        "root_users": others.get("ROOT_User", []),
        "auto_approval": others.get("Auto_approval", []),
        "emoji_plus_one_enabled": others.get("emoji_plus_one_enabled", True),
        "emoji_plus_one_cooldown_seconds": others.get("emoji_plus_one_cooldown_seconds", 1.0),
        "poke_reply_enabled": others.get("poke_reply_enabled", True),
        "poke_cooldown_seconds": others.get("poke_cooldown_seconds", 8),
        "api_request_timeout_seconds": others.get("api_request_timeout_seconds", 60),
        "summary_per_day_limit": others.get("summary_per_day_limit", 1),
        "summary_max_messages": others.get("summary_max_messages", 200),
        "context_max_messages": others.get("context_max_messages", 60),
        "compression_threshold": others.get("compression_threshold", 40),
        "compression_keep_recent": others.get("compression_keep_recent", 20),
        "auto_compress_after_messages": others.get("auto_compress_after_messages", 40),
        "weak_blacklist_trigger_probability": others.get("weak_blacklist_trigger_probability", 0.3),
        "weak_blacklist_users": others.get("weak_blacklist_users", []),
        "group_random_reply_probability": others.get("group_random_reply_probability", 0),
        "group_random_reply_quote": others.get("group_random_reply_quote", False),
        "llm_endpoints": others.get("llm_endpoints", []),
        "llm_providers": others.get("llm_providers", []),
        "llm_rotation": others.get("llm_rotation", []),
        "api_failure_cooldown_seconds": others.get("api_failure_cooldown_seconds", 5),
        "api_default_index": others.get("api_default_index", 1),
        "api_default_model": others.get("api_default_model", ""),
        "api_multimodal_model": others.get("api_multimodal_model", ""),
        "api_multimodal_image_mode": others.get("api_multimodal_image_mode", "relay"),
        "_comment_api_multimodal_model": others.get("_comment_api_multimodal_model", "多模态转述模型。当主模型不支持多模态且用户发送图片时，使用这里填写的多模态模型识图转述；留空则保持原行为，不额外调用多模态模型。"),
        "personality_prompt": others.get("personality_prompt", ""),
        "sensitive_words": others.get("sensitive_words", []),
        "llm_reply_failover_keywords": others.get("llm_reply_failover_keywords", []),
    }
    return defaults


user_cfg = load_user_cfg()


def read_runtime_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"读取运行时配置失败: {e}")
        return {}


from bot.io_json import atomic_write_json


def write_runtime_config(data: dict) -> bool:
    """统一原子写入唯一配置文件 config.json。"""
    try:
        atomic_write_json(CONFIG_FILE, data, indent=4)
        return True
    except Exception as e:
        print(f"写入运行时配置失败: {e}")
        return False


def get_runtime_others() -> dict:
    cfg = read_runtime_config()
    others = cfg.get("Others", {})
    return others if isinstance(others, dict) else {}


def get_feature_switches() -> dict:
    cfg = read_runtime_config()
    raw = cfg.get("FeatureSwitches", {})
    switches = dict(DEFAULT_FEATURE_SWITCHES)
    if isinstance(raw, dict):
        for key in list(switches.keys()):
            if key in raw:
                switches[key] = normalize_bool_config(raw.get(key), default=switches[key])
    return switches


def is_feature_enabled(key: str, default: bool = True) -> bool:
    return normalize_bool_config(get_feature_switches().get(key, default), default=default)


def get_sensitive_words_mapping() -> dict[str, str]:
    raw_items = get_runtime_others().get("sensitive_words", user_cfg.get("sensitive_words", []))
    mapping: dict[str, str] = {}

    if isinstance(raw_items, dict):
        for key, value in raw_items.items():
            key_text = str(key or "").strip()
            if key_text:
                mapping[key_text] = str(value or "")
        return mapping

    if isinstance(raw_items, list):
        for item in raw_items:
            text = str(item or "").strip()
            if not text:
                continue
            if "=" in text:
                key_text, value_text = text.split("=", 1)
                key_text = key_text.strip()
                if key_text:
                    mapping[key_text] = value_text.strip()
            else:
                mapping[text] = ""

    return mapping


def get_llm_reply_failover_keywords() -> list[str]:
    raw_items = get_runtime_others().get(
        "llm_reply_failover_keywords",
        user_cfg.get("llm_reply_failover_keywords", []),
    )
    if isinstance(raw_items, str):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        return []

    result: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def find_llm_reply_failover_keyword(text: str) -> str:
    reply = str(text or "")
    if not reply:
        return ""

    lower_reply = reply.lower()
    for keyword in get_llm_reply_failover_keywords():
        if keyword.lower() in lower_reply:
            return keyword
    return ""


def get_runtime_setting(path: str, default=None):
    current = read_runtime_config()
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def get_llm_split_config() -> dict:
    others = get_runtime_others()
    cfg = others.get("llm_split", {})
    if not isinstance(cfg, dict):
        cfg = {}
    enabled = normalize_bool_config(cfg.get("enabled", False), default=False)
    mode = str(cfg.get("mode", "auto_prompt") or "auto_prompt").strip() or "auto_prompt"
    if mode not in {"auto_prompt", "regex"}:
        mode = "auto_prompt"
    try:
        max_chars_no_split = int(cfg.get("max_chars_no_split", 0) or 0)
    except (TypeError, ValueError):
        max_chars_no_split = 0
    return {
        "enabled": enabled,
        "mode": mode,
        "prompt_suffix": str(cfg.get("prompt_suffix", "") or "").strip(),
        "split_regex": str(cfg.get("split_regex", r".*?[。？！~]+|.+$") or r".*?[。？！~]+|.+$").strip(),
        "filter_regex": str(cfg.get("filter_regex", r"\n|\r") or r"\n|\r").strip(),
        "max_chars_no_split": max(0, max_chars_no_split),
    }


def build_llm_user_message(message: str) -> str:
    return str(message or "")


def build_llm_system_prompt(system_prompt: str) -> str:
    prompt = str(system_prompt or "")
    cfg = get_llm_split_config()
    if not cfg.get("enabled"):
        return prompt
    if cfg.get("mode") != "auto_prompt":
        return prompt
    suffix = str(cfg.get("prompt_suffix", "") or "").strip()
    if not suffix:
        return prompt
    if not prompt:
        return suffix
    return f"{prompt}\n\n{suffix}"


def split_llm_reply_for_send(ai_reply: str) -> list[str]:
    text = str(ai_reply or "")
    cfg = get_llm_split_config()
    enabled = bool(cfg.get("enabled"))
    mode = cfg.get("mode", "auto_prompt")
    filter_regex = cfg.get("filter_regex", r"\n|\r")
    max_chars_no_split = int(cfg.get("max_chars_no_split", 0) or 0)
    split_marker_pattern = r'<\s*split\s*>'

    def _clean_reply_part(part: str) -> str:
        cleaned = str(part or "")
        cleaned = re.sub(split_marker_pattern, "", cleaned, flags=re.IGNORECASE)
        if filter_regex:
            try:
                cleaned = re.sub(filter_regex, "", cleaned)
            except re.error as e:
                print(f"[LLM Split] 过滤正则配置无效，已跳过过滤: {e}")
        return cleaned.strip()

    ai_reply_cleaned = re.sub(split_marker_pattern, '<split>', text, flags=re.IGNORECASE)
    split_marker = "<split>"
    single_text = _clean_reply_part(ai_reply_cleaned)

    # 当整条消息长度超过阈值时，直接作为单条发送，不做分段。
    # 长度按过滤换行等清理后的最终文本计算，避免 <split> 标记影响判断。
    whole_text = single_text
    if max_chars_no_split > 0 and len(whole_text) > max_chars_no_split:
        return [whole_text] if whole_text else []

    # 关闭分段时：不做任何分段，但仍全局过滤掉 <split> 标记。
    if not enabled:
        single = single_text
        return [single] if single else []

    # 自动提示词分段：仅当模型实际输出 <split> 时按 <split> 分段。
    if mode == "auto_prompt" and split_marker in ai_reply_cleaned:
        parts = [p for p in (_clean_reply_part(x) for x in ai_reply_cleaned.split(split_marker)) if p]
        if parts:
            return parts

    # 正则分段：忽略 <split> 的语义，只把它当作需过滤的脏标记。
    if mode == "regex":
        split_regex = cfg.get("split_regex", r".*?[。？！~]+|.+$")
        try:
            regex_source = re.sub(split_marker_pattern, "", text, flags=re.IGNORECASE)
            raw_parts = re.findall(split_regex, regex_source, flags=re.S)
            parts = []
            for item in raw_parts:
                part = _clean_reply_part(item)
                if part:
                    parts.append(part)
            if parts:
                return parts
        except re.error as e:
            print(f"[LLM Split] 正则分段配置无效，已回退到单条发送: {e}")

    single = single_text
    return [single] if single else []


def normalize_probability_config(value, default: float = 0.3) -> float:
    """兼容 0~1 和 0~100 两种概率写法。"""
    try:
        probability = float(value)
    except (TypeError, ValueError):
        probability = float(default)

    if probability > 1:
        probability = probability / 100.0

    return max(0.0, min(1.0, probability))


def normalize_seconds_config(value, default: float = 8.0, minimum: float = 0.0) -> float:
    """兼容字符串/数字秒数配置，避免异常值导致冷却失效。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = float(default)
    return max(float(minimum), seconds)


def normalize_bool_config(value, default: bool = False) -> bool:
    """兼容 WebUI/手写配置中的布尔值，避免字符串 "false" 被 bool() 误判为 True。"""
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



from bot.llm_config import normalize_legacy_endpoints as normalize_llm_endpoints
from bot.llm_config import normalize_llm_provider_rotation

def build_openai_message_content(text: str, image_urls: list[str] | None = None, supports_multimodal: bool = False):
    safe_text = str(text or "").strip()
    urls = [
        str(url).strip()
        for url in (image_urls or [])
        if str(url).strip().startswith("http") or str(url).strip().startswith("data:")
    ]
    if not supports_multimodal or not urls:
        return safe_text

    content = []
    if safe_text:
        content.append({"type": "text", "text": safe_text})
    for image_url in urls:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    return content or safe_text


def extract_image_url_from_segment(segment) -> str:
    candidates = []
    for attr in ("url", "file"):
        value = getattr(segment, attr, None)
        if value:
            candidates.append(value)

    data = getattr(segment, "data", None)
    if isinstance(data, dict):
        candidates.extend([data.get("url"), data.get("file")])

    raw = getattr(segment, "raw", None)
    if isinstance(raw, dict):
        raw_data = raw.get("data", raw)
        if isinstance(raw_data, dict):
            candidates.extend([raw_data.get("url"), raw_data.get("file")])

    for value in candidates:
        if isinstance(value, str) and value.strip().startswith("http"):
            return value.strip()

    text = str(segment)
    match = re.search(r'https?://[^\s\'"<>]+', text)
    return match.group(0) if match else ""


def extract_image_urls_from_message(message) -> list[str]:
    urls = []
    try:
        for segment in message:
            if isinstance(segment, Segments.Image):
                url = extract_image_url_from_segment(segment)
                if url:
                    urls.append(url)
    except Exception:
        pass
    return urls


def replace_scheme_with_http(url: str) -> str:
    try:
        parsed_url = urllib.parse.urlparse(str(url or "").strip())
        if parsed_url.scheme == "https":
            parsed_url = parsed_url._replace(scheme="http")
        return urllib.parse.urlunparse(parsed_url)
    except Exception:
        return str(url or "").strip()


async def convert_image_url_to_data_url(url: str) -> str:
    raw_url = str(url or "").strip()
    if not raw_url:
        return ""
    if raw_url.startswith("data:"):
        return raw_url

    allowed_schemes = {"http", "https"}
    try:
        parsed = urllib.parse.urlparse(raw_url)
    except Exception:
        return raw_url
    if parsed.scheme not in allowed_schemes:
        return raw_url

    # SSRF 防护：禁止内网 / link-local / metadata
    host = (parsed.hostname or "").strip()
    if not host:
        return raw_url
    if host.lower() in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return raw_url
    try:
        if ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback or ipaddress.ip_address(host).is_link_local or ipaddress.ip_address(host).is_reserved:
            return raw_url
    except ValueError:
        pass

    candidates = [raw_url]
    http_url = replace_scheme_with_http(raw_url)
    if http_url and http_url != raw_url:
        candidates.append(http_url)

    timeout = aiohttp.ClientTimeout(total=20)
    last_error = None
    max_bytes = 8 * 1024 * 1024
    for candidate in candidates:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(candidate, headers={"User-Agent": "Mozilla/5.0"}) as response:
                    if response.status != 200:
                        last_error = f"HTTP {response.status}"
                        continue
                    data = b""
                    async for chunk in response.content.iter_any():
                        data += chunk
                        if len(data) > max_bytes:
                            last_error = "image too large (>8MB)"
                            break
                    else:
                        if not data:
                            last_error = "empty body"
                            continue
                        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                        if not content_type or not content_type.startswith("image/"):
                            guessed, _ = mimetypes.guess_type(candidate)
                            content_type = guessed or "image/jpeg"
                        encoded = base64.b64encode(data).decode("utf-8")
                        return f"data:{content_type};base64,{encoded}"
                    continue
        except Exception as e:
            last_error = str(e)
            continue

    print(f"[DEBUG] 图片转 data URL 失败: src={raw_url[:160]} err={last_error}")
    return raw_url


async def prepare_image_inputs_for_model(image_urls: list[str], supports_multimodal: bool) -> list[str]:
    urls = [str(url).strip() for url in (image_urls or []) if str(url).strip()]
    if not supports_multimodal or not urls:
        return []

    prepared = []
    for url in urls:
        prepared.append(await convert_image_url_to_data_url(url))
    return prepared



def get_configured_multimodal_model() -> str:
    others = get_runtime_others()
    return str(others.get("api_multimodal_model", user_cfg.get("api_multimodal_model", "")) or "").strip()


def get_multimodal_image_mode() -> str:
    value = str(get_runtime_others().get(
        "api_multimodal_image_mode",
        user_cfg.get("api_multimodal_image_mode", "relay"),
    ) or "relay").strip().lower()
    return value if value in {"relay", "direct"} else "relay"


def build_image_relay_prompt(user_text: str) -> str:
    text = str(user_text or "").strip()
    if not text:
        text = "用户只发送了图片，没有额外文字。"
    return (
        "请识别并转述用户发送的图片内容，输出给另一个不支持看图的主模型使用。\n"
        "要求：用中文，客观描述图片里的主体、文字、场景、表情、动作和任何与用户文字相关的信息；"
        "不要替用户回答问题，不要扩写成聊天回复，只提供主模型理解图片所需的信息。\n\n"
        f"用户随图文字：{text}"
    )


async def relay_images_with_multimodal_model(context, user_text: str, image_urls: list[str]) -> tuple[str, int, int, int]:
    urls = [str(url).strip() for url in (image_urls or []) if str(url).strip()]
    if not urls:
        return "", 0, 0, 0

    preferred_model = get_configured_multimodal_model()
    if not preferred_model:
        return "", 0, 0, 0

    max_retries = key_manager.get_attempt_count() or 1
    tried_keys = set()
    last_error = None

    for _ in range(max_retries):
        current = key_manager.get_next_multimodal_for_request(
            tried_keys=tried_keys,
            include_cooldown=True,
            preferred_model=preferred_model,
        )
        if not current:
            break

        base_url, current_key, model, supports_multimodal, timeout_seconds, display_model = current
        tried_keys.add(key_manager.make_attempt_identity(base_url, current_key, model))

        try:
            prepared_urls = await prepare_image_inputs_for_model(urls, supports_multimodal=True)
            if not prepared_urls:
                return "", 0, 0, 0

            messages = [
                {"role": "system", "content": "你是图片转述助手，只负责把图片信息准确转成文字，供不支持多模态的主模型继续处理。"},
                {
                    "role": "user",
                    "content": build_openai_message_content(
                        build_image_relay_prompt(user_text),
                        image_urls=prepared_urls,
                        supports_multimodal=True,
                    ),
                },
            ]

            scene = f"{getattr(context, 'session_id', 'AI')}:vision"
            log_api_request(
                scene=scene,
                model=display_model,
                base_url=base_url,
                current_key=current_key,
                message_count=len(messages),
                preview="图片转述",
            )

            client = context._get_client(base_url, current_key, timeout_seconds)
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.chat.completions.create,
                        model=model,
                        messages=messages,
                        stream=False,
                        timeout=timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                raise Exception(f"多模态转述请求超过 {timeout_seconds} 秒未返回")

            if response is None or not getattr(response, "choices", None):
                raise Exception("多模态转述 API 返回异常，choices 为空")

            description = (response.choices[0].message.content or "").rstrip("\n")
            if not description.strip():
                raise Exception("多模态转述结果为空")

            usage = getattr(response, "usage", None)
            total_tokens = getattr(usage, "total_tokens", 0) if usage else 0
            prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
            completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

            key_manager.mark_success(current_key, model=model, base_url=base_url)
            log_api_success(scene=scene, model=display_model, total_tokens=total_tokens, reply=description)
            return description, total_tokens, prompt_tokens, completion_tokens

        except Exception as e:
            scene = f"{getattr(context, 'session_id', 'AI')}:vision"
            log_api_failure(scene, display_model, current_key, error=str(e))
            key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
            last_error = e
            continue

    if last_error:
        print(f"[Vision Relay] 多模态转述失败，将按原始纯文本请求继续: {last_error}")
    elif preferred_model:
        print(f"[Vision Relay] 未找到配置的多模态模型或可用 key: {preferred_model}")
    return "", 0, 0, 0


def merge_image_relay_into_user_content(user_content: str, image_description: str) -> str:
    base = str(user_content or "").strip()
    desc = str(image_description or "").strip()
    if not desc:
        return base
    relay_text = f"[图片转述]\n{desc}"
    if base:
        return f"{base}\n\n{relay_text}"
    return relay_text


def build_private_ai_text_message(event_user_nickname: str, text: str) -> str:
    return f"【{event_user_nickname}】说：{filter_sensitive_content(str(text or '').strip())}"


def build_group_ai_text_message(event_user_nickname: str, text: str, is_at_trigger: bool = False) -> str:
    cleaned = filter_sensitive_content(str(text or '').strip())
    if is_at_trigger and not cleaned:
        return f"【{event_user_nickname}】艾特了你"
    return f"【{event_user_nickname}】说：{cleaned}"


def apply_api_rotation_settings(cfg: dict = None, verbose: bool = True) -> list:
    """应用提供商/模型轮换设置。"""
    cfg = cfg or user_cfg
    endpoints_config = normalize_llm_provider_rotation(cfg)
    if not endpoints_config:
        fallback_cfg = dict(cfg)
        fallback_cfg["llm_providers"] = getattr(config, "others", {}).get("llm_providers", [])
        fallback_cfg["llm_rotation"] = getattr(config, "others", {}).get("llm_rotation", [])
        fallback_cfg["llm_endpoints"] = getattr(config, "others", {}).get("llm_endpoints", [])
        endpoints_config = normalize_llm_provider_rotation(fallback_cfg)

    if not endpoints_config:
        if verbose:
            print("警告: 未配置任何 API 端点，AI 功能将不可用")
        key_manager.set_endpoints([])
        return []

    key_manager.set_endpoints(endpoints_config)

    if verbose:
        total_keys = sum(len(ep.get("keys", [])) for ep in endpoints_config)
        print(f"已加载 {len(endpoints_config)} 个模型轮换项、{total_keys} 个 API Key")
        print(f"当前轮换项: {key_manager.get_current_display()}")
        print("模型轮换列表：")
        for i, ep in enumerate(endpoints_config, start=1):
            print(f"  [{i}] 模型: {ep.get('display_model', ep['model'])} | 地址: {ep['base_url']} | Key 数量: {len(ep.get('keys', []))} | 超时: {ep.get('timeout_seconds', 60)}s")

    return endpoints_config


def close_runtime_llm_clients():
    """配置热更新时关闭并清空全部共享 OpenAI 客户端池。"""
    for cls in (globals().get("LimitedDeepSeekContext"), globals().get("ContextCompressor")):
        pool = getattr(cls, "_client_pool", None) if cls is not None else None
        if not isinstance(pool, dict):
            continue
        clients = list(pool.values())
        pool.clear()
        for client in clients:
            try:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass


def get_api_failure_cooldown_seconds() -> int:
    """读取 API 失败后的冷却秒数。"""
    try:
        value = get_runtime_setting("Others.api_failure_cooldown_seconds", user_cfg.get("api_failure_cooldown_seconds", 5))
        value = int(value)
    except (TypeError, ValueError):
        value = 5
    return max(1, value)


def get_connection_signature(cfg=None) -> dict:
    """生成连接配置快照，优先读取 config.json 原始 Connection，避免 Configurator 内部对象复用或延迟刷新。"""
    if isinstance(cfg, dict):
        connection_cfg = cfg.get("Connection", {}) if isinstance(cfg.get("Connection", {}), dict) else {}
        return {
            "protocol": str(cfg.get("protocol", "") or ""),
            "mode": str(connection_cfg.get("mode", "") or ""),
            "host": str(connection_cfg.get("host", "") or ""),
            "port": str(connection_cfg.get("port", "") or ""),
            "listener_host": str(connection_cfg.get("listener_host", "") or ""),
            "listener_port": str(connection_cfg.get("listener_port", "") or ""),
            "retries": str(connection_cfg.get("retries", "") or ""),
        }

    cfg = cfg or config
    connection_cfg = getattr(cfg, "connection", None)
    return {
        "protocol": str(getattr(cfg, "protocol", "") or ""),
        "mode": str(getattr(connection_cfg, "mode", "") or ""),
        "host": str(getattr(connection_cfg, "host", "") or ""),
        "port": str(getattr(connection_cfg, "port", "") or ""),
        "listener_host": str(getattr(connection_cfg, "listener_host", "") or ""),
        "listener_port": str(getattr(connection_cfg, "listener_port", "") or ""),
        "retries": str(getattr(connection_cfg, "retries", "") or ""),
    }


RUNTIME_CONNECTION_SNAPSHOT = get_connection_signature(read_runtime_config())
HOT_SWITCH_IN_PROGRESS = threading.Event()


def apply_listener_connection_hot_update(new_cfg) -> None:
    """对 Hyper.Listener 做真正的运行时连接切换。

    Hyper.Listener 通过 ``from Hyper.Adapters.OneBot import *`` 暴露 ``run/stop``，
    因此 ``Listener.run`` 执行时实际读取的是适配器模块（例如
    ``Hyper.Adapters.OneBot``）自己的全局 ``config/connection``，而不是
    ``Hyper.Listener.config``。旧实现只替换了 ``Listener.connection``，运行中的
    ``run`` 循环不会使用这份对象，导致 WebUI 修改连接地址/端口后仍按旧配置重连。

    正确做法是：刷新 Listener 与实际适配器模块中的配置，然后关闭当前连接，
    让 Hyper 原本的 ``run`` 外层循环用新配置重新创建并连接。这样不会额外启动
    第二个监听循环，也能保留已注册的 handler。
    """
    try:
        runtime_cfg = read_runtime_config()
        connection_raw = runtime_cfg.get("Connection", {}) if isinstance(runtime_cfg.get("Connection", {}), dict) else {}
        protocol = str(runtime_cfg.get("protocol", getattr(new_cfg, "protocol", "")) or "").strip() or "OneBot"

        host = str(connection_raw.get("host", getattr(getattr(new_cfg, "connection", None), "host", "")) or "").strip()
        port = int(connection_raw.get("port", getattr(getattr(new_cfg, "connection", None), "port", 0)) or 0)
        listener_host = str(connection_raw.get("listener_host", getattr(getattr(new_cfg, "connection", None), "listener_host", host)) or host).strip()
        listener_port = int(connection_raw.get("listener_port", getattr(getattr(new_cfg, "connection", None), "listener_port", port)) or port)
        mode = str(connection_raw.get("mode", getattr(getattr(new_cfg, "connection", None), "mode", "FWS")) or "FWS").strip().upper()

        if not host or not port:
            raise RuntimeError(f"缺少有效连接配置: host={host!r}, port={port!r}")

        adapter_module = sys.modules.get(getattr(Listener.run, "__module__", ""))
        if adapter_module is None:
            adapter_module = importlib.import_module(getattr(Listener.run, "__module__", "Hyper.Adapters.OneBot"))

        current_adapter_protocol = "Satori" if adapter_module.__name__.endswith(".Satori") else "OneBot"
        if protocol != current_adapter_protocol:
            raise RuntimeError(
                "当前 Hyper.Listener 已加载 "
                f"{current_adapter_protocol} 适配器，运行时不支持切换到 {protocol}；"
                "请重启程序后生效。"
            )

        if protocol == "OneBot" and mode not in {"FWS", "HTTP", "HTTP_POST", "POST"}:
            raise RuntimeError(f"不支持的 OneBot 连接模式: {mode}")
        if protocol == "Satori" and mode != "FWS":
            raise RuntimeError(f"Satori 运行时只支持 FWS 模式: {mode}")

        HOT_SWITCH_IN_PROGRESS.set()

        # 同步 Configurator、Listener 门面模块以及实际适配器模块中的配置引用。
        Configurator.cm = Configurator.ConfigManager(Configurator.Config(file=CONFIG_FILE).load_from_file())
        refreshed_cfg = Configurator.cm.get_cfg()
        globals()["config"] = refreshed_cfg
        Listener.Configurator.cm = Configurator.cm
        Listener.config = refreshed_cfg
        if hasattr(adapter_module, "Configurator"):
            adapter_module.Configurator.cm = Configurator.cm
        adapter_module.config = refreshed_cfg
        if hasattr(adapter_module, "logger"):
            try:
                adapter_module.logger.set_level(refreshed_cfg.log_level)
            except Exception:
                pass

        # 关闭实际适配器持有的旧连接。Listener.run 外层循环会随后按新配置创建连接。
        old_connection = getattr(adapter_module, "connection", None)
        if old_connection is not None:
            try:
                old_connection.close()
            except Exception as close_error:
                print(f"关闭旧连接时出现异常（通常可忽略）: {close_error}")
        else:
            try:
                Listener.stop()
            except Exception:
                pass

        print(
            "✅ 已对 Hyper.Listener 应用热连接配置: "
            f"{protocol} {mode} {host}:{port}"
            + (f"，监听 {listener_host}:{listener_port}" if listener_host and listener_port and mode != "FWS" else "")
        )
    except Exception as e:
        HOT_SWITCH_IN_PROGRESS.clear()
        raise RuntimeError(f"热切换 Listener 连接失败: {e}") from e


def apply_runtime_config() -> bool:
    global config, user_cfg, bot_name, bot_name_en, project_name, version_name, reminder
    global ONE_SLOGAN, ROBOT_NAME_TRIGGERS, ROOT_User, Super_User, Manage_User
    global POKE_COOLDOWN_SECONDS, POKE_REPLY_ENABLED, EMOJI_PLUS_ONE_ENABLED, EMOJI_PLUS_ONE_COOLDOWN_SECONDS
    global API_REQUEST_TIMEOUT_SECONDS, SUMMARY_PER_DAY_LIMIT, SUMMARY_MAX_MESSAGES, sys_prompt
    global RUNTIME_CONNECTION_SNAPSHOT
    try:
        old_connection = dict(RUNTIME_CONNECTION_SNAPSHOT)

        raw_runtime_cfg = read_runtime_config()

        Configurator.cm = Configurator.ConfigManager(Configurator.Config(file=CONFIG_FILE).load_from_file())
        config = Configurator.cm.get_cfg()
        user_cfg = load_user_cfg()

        new_connection = get_connection_signature(raw_runtime_cfg)
        RUNTIME_CONNECTION_SNAPSHOT = dict(new_connection)

        bot_name = user_cfg["bot_name"]
        bot_name_en = user_cfg["bot_name_en"]
        project_name = user_cfg.get("project_name", "XcBot")
        version_name = user_cfg["version_name"]
        reminder = user_cfg["reminder"]
        ONE_SLOGAN = user_cfg.get("slogan", "✨ 忻城 ✨")
        sys_prompt = str(user_cfg.get("personality_prompt", "") or "")
        ROBOT_NAME_TRIGGERS = [str(x) for x in user_cfg.get("robot_name_triggers", [bot_name]) if str(x).strip()]
        admin_users = user_cfg.get("root_users", [])
        ROOT_User = [str(x).strip() for x in admin_users if str(x).strip()]
        if "load_admin_lists_from_config" in globals():
            admin_users, _ = load_admin_lists_from_config()
            ROOT_User = admin_users[:]
            Super_User = admin_users[:]
            Manage_User = admin_users[:]

        POKE_COOLDOWN_SECONDS = normalize_seconds_config(user_cfg.get("poke_cooldown_seconds", 8), default=8.0)
        POKE_REPLY_ENABLED = normalize_bool_config(user_cfg.get("poke_reply_enabled", True), default=True)
        EMOJI_PLUS_ONE_ENABLED = normalize_bool_config(user_cfg.get("emoji_plus_one_enabled", True), default=True)
        EMOJI_PLUS_ONE_COOLDOWN_SECONDS = float(user_cfg.get("emoji_plus_one_cooldown_seconds", 1.0))

        API_REQUEST_TIMEOUT_SECONDS = int(user_cfg.get("api_request_timeout_seconds", 60))
        SUMMARY_PER_DAY_LIMIT = int(user_cfg.get("summary_per_day_limit", 1))
        SUMMARY_MAX_MESSAGES = int(user_cfg.get("summary_max_messages", 200))

        apply_api_rotation_settings(user_cfg, verbose=True)
        close_runtime_llm_clients()

        logger.set_level(config.log_level)

        if 'cmc' in globals() and hasattr(cmc, 'compressor'):
            cmc.compressor.keep_recent = int(user_cfg.get("compression_keep_recent", 20))
            cmc.compressor.compression_threshold = int(user_cfg.get("compression_threshold", 40))
            # 热更新已有会话：人设、上下文上限、自动压缩阈值
            new_max_messages = int(user_cfg.get("context_max_messages", 60))
            new_auto_compress = int(user_cfg.get("auto_compress_after_messages", 40))
            for ctx in list(getattr(cmc, "private_chats", {}).values()) + list(getattr(cmc, "groups", {}).values()):
                try:
                    if sys_prompt:
                        # 尽量保留 {bot_name}/{user_name} 语义：用当前全局 bot_name 替换
                        prompt = str(sys_prompt).replace("{bot_name}", bot_name)
                        if getattr(ctx, "context_type", "") == "private" and getattr(ctx, "chat_id", None) is not None:
                            prompt = prompt.replace("{user_name}", f"用户{ctx.chat_id}")
                        else:
                            prompt = prompt.replace("{user_name}", "群聊会话")
                        ctx.system_prompt = filter_sensitive_content(prompt)
                    ctx.max_messages = new_max_messages
                    if hasattr(ctx, "compress_after_messages"):
                        ctx.compress_after_messages = new_auto_compress
                    if hasattr(ctx, "_enforce_message_limit"):
                        ctx._enforce_message_limit()
                except Exception:
                    pass

        if is_feature_enabled("plugins_external", True):
            try:
                globals()['plugins'] = load_plugins()
            except Exception as e:
                print(f"热加载外部插件失败: {e}")
        else:
            globals()['plugins'] = []
            loaded_plugins.clear()
            disabled_plugins.clear()
            failed_plugins.clear()

        if old_connection != new_connection:
            endpoint_text = f"{new_connection['host']}:{new_connection['port']}"
            print(f"🔄 检测到连接配置变更，准备自动重启进程应用新连接: {old_connection} -> {new_connection}")
            set_connection_status("connecting", "重启中", f"连接配置已更新，正在切换到 {new_connection['protocol']} · {endpoint_text}")

            def _hot_switch_listener_connection():
                try:
                    time.sleep(0.5)
                    restart_current_process(f"连接配置已更新，切换到 {new_connection['protocol']} · {endpoint_text}")
                except Exception as restart_error:
                    print(f"连接配置变更后自动重启失败: {restart_error}")
                    set_connection_status("failed", "连接切换失败", str(restart_error))

            threading.Thread(target=_hot_switch_listener_connection, name="config-hot-switch", daemon=True).start()

        print("✅ 运行时配置已热更新")
        return True
    except Exception as e:
        print(f"应用运行时配置失败: {e}")
        traceback.print_exc()
        return False

# ==================== 日志配置 ====================
logger = Logger.Logger()
logger.set_level(config.log_level)

# ==================== 全局常量 ====================
bot_name = user_cfg["bot_name"]
bot_name_en = user_cfg["bot_name_en"]
project_name = user_cfg.get("project_name", "XcBot")
version_name = user_cfg["version_name"]
reminder: str = user_cfg["reminder"]
ONE_SLOGAN: str = user_cfg.get("slogan", "✨ 忻城 ✨")
sys_prompt: str = str(user_cfg.get("personality_prompt", "") or "")
ROBOT_NAME_TRIGGERS = [str(x) for x in user_cfg.get("robot_name_triggers", [bot_name]) if str(x).strip()]

# main.py 里初始化 API Key 管理器这一段，完整替换
# ==================== 初始化 API Key 管理器（支持多端点和多模型） ====================
endpoints_config = apply_api_rotation_settings(user_cfg, verbose=True)


# ==================== 单目录单实例锁 ====================
# 锁文件放在项目目录内（BASE_DIR），而不是 cwd 或 /tmp 的全局路径。
# 效果：同一个目录只能跑一个 Bot；复制成多份目录各跑一个互不影响。
LOCK_FILE = str(BASE_DIR / "my_bot.lock")
# Windows 下被 msvcrt.locking 锁住的文件整个不可读，所以 PID 写在旁边的独立文件里，
# 这样被拒的实例还能读出是谁占着目录。
LOCK_PID_FILE = str(BASE_DIR / "my_bot.pid")
lock_fp = None


def _read_lock_holder() -> str:
    """读 PID 文件，仅用于提示是谁占着目录。"""
    try:
        with open(LOCK_PID_FILE, "r", encoding="utf-8") as f:
            text = f.read(64).strip()
        return f"PID {text}" if text.isdigit() else "未知进程"
    except Exception:
        return "未知进程"


def _acquire_single_instance_lock():
    """获取当前目录的独占锁；已被占用则退出。

    Windows 用 msvcrt.locking 锁首字节，Linux/macOS 用 fcntl.flock 锁整个文件。
    两者都由内核在进程退出时自动释放，所以崩溃残留的锁文件不会挡住下次启动。
    """
    global lock_fp
    try:
        lock_fp = open(LOCK_FILE, "a+", encoding="utf-8")
    except OSError as e:
        # 目录不可写属于异常部署（config.json / data 也写不了），
        # 这里只告警不退出，避免因为锁本身让机器人完全起不来。
        lock_fp = None
        print(f"⚠️ 无法创建锁文件，已跳过单实例检查：{e}")
        print(f"   路径: {LOCK_FILE}")
        return

    holder = _read_lock_holder()
    try:
        if sys.platform == "win32":
            import msvcrt
            # 显式定位到 0：'a+' 打开已有文件时读写位置可能在末尾，
            # 不固定的话加锁和解锁会落在不同字节上。
            lock_fp.seek(0)
            msvcrt.locking(lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            lock_fp.close()
        except Exception:
            pass
        lock_fp = None
        print(f"❌ 本目录已有一个实例在运行（{holder}），退出")
        print(f"   目录: {BASE_DIR}")
        print("   如需多开，请把项目复制到另一个目录，并改用不同的 Connection.port 和 WebUI.port")
        sys.exit(1)

    try:
        with open(LOCK_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    print(f"✅ 目录独占锁获取成功, PID: {os.getpid()}")


def release_lock():
    """释放锁。os.execv 重启前需手动调用（不触发 atexit）。

    刻意不删 my_bot.lock：POSIX 下删锁文件有竞态——A 释放并 unlink 的瞬间，
    B 可能已打开旧 inode，C 又新建一个 inode，两者各自 flock 成功，
    同目录就会跑起两个实例。留一个 0 字节文件最安全。
    """
    global lock_fp
    if not lock_fp:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            try:
                lock_fp.seek(0)
                msvcrt.locking(lock_fp.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(lock_fp, fcntl.LOCK_UN)
            except OSError:
                pass
        lock_fp.close()
    except Exception as e:
        print(f"释放锁时出错：{e}")
    finally:
        lock_fp = None
        try:
            os.remove(LOCK_PID_FILE)
        except OSError:
            pass
        print("✅ 目录独占锁已释放")


_acquire_single_instance_lock()
atexit.register(release_lock)


# ==================== 全局变量 ====================
poke_cooldowns = {}
POKE_COOLDOWN_SECONDS = normalize_seconds_config(user_cfg.get("poke_cooldown_seconds", 8), default=8.0)
POKE_REPLY_ENABLED = normalize_bool_config(user_cfg.get("poke_reply_enabled", True), default=True)
EMOJI_PLUS_ONE_ENABLED = normalize_bool_config(user_cfg.get("emoji_plus_one_enabled", True), default=True)
EMOJI_PLUS_ONE_COOLDOWN_SECONDS = float(user_cfg.get("emoji_plus_one_cooldown_seconds", 1.0))
second_start = time.time()
EnableNetwork = "Pixmap"
user_lists = {}
settings_loaded = False
emoji_send_count: dict = {}
generating = False
running = True  # 机器人运行标志

# HTTP 出站发送桥：跨线程保存当前 Hyper Actions
_current_qq_actions = None
_current_bot_self_id = None
_qq_actions_lock = threading.RLock()
_qq_send_lock = threading.Lock()


def _emoji_cooldown_seconds(user_id: str, is_group: bool = False, group_id: str = "") -> float:
    """表情 +1 冷却秒数：优先个人配置，其次全局。"""
    per_user = get_runtime_setting(f"Others.emoji_plus_one_cooldown_seconds_map.{user_id}", None)
    try:
        if per_user is not None:
            return max(0.0, float(per_user))
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, float(get_runtime_setting("Others.emoji_plus_one_cooldown_seconds", EMOJI_PLUS_ONE_COOLDOWN_SECONDS)))
    except (TypeError, ValueError):
        return 1.0


def _is_emoji_plus_one_available(user_id: str, is_group: bool = False, group_id: str = "") -> bool:
    cooldown_key = f"emoji_plus_one:{user_id}"
    last_emoji_time = emoji_send_count.get(cooldown_key)
    now = datetime.datetime.now()
    if last_emoji_time is None or (now - last_emoji_time).total_seconds() >= _emoji_cooldown_seconds(user_id, is_group, group_id):
        emoji_send_count[cooldown_key] = now
        return True
    return False


# ==================== 权限列表 ====================
ROOT_User: list = user_cfg.get("root_users", [])
Super_User: list = []
Manage_User: list = []

# ==================== 插件系统全局变量 ====================
import importlib.util
import inspect
PLUGIN_FOLDER = "plugins"
if not os.path.exists(PLUGIN_FOLDER):
    os.makedirs(PLUGIN_FOLDER)

loaded_plugins = []      # 已加载的插件模块名（带唯一标识）
disabled_plugins = []    # 被禁用的插件原始名
failed_plugins = []      # 加载失败的插件名
plugins = []             # 插件模块对象列表
plugins_help = ""        # 插件帮助信息汇总

LEGACY_CONFIG_FILES = [
    os.path.join(os.getcwd(), "Manage_User.ini"),
    os.path.join(os.getcwd(), "Super_User.ini"),
    os.path.join(os.getcwd(), "blacklist.sr"),
    os.path.join(PLUGIN_FOLDER, "split_reply_quote.json"),
]


def cleanup_legacy_config_files() -> None:
    """删除历史遗留配置文件，避免运行时再次误读或被误判为保存目标。"""
    for path in LEGACY_CONFIG_FILES:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def load_split_reply_quote_settings() -> dict:
    """从 config.json 加载分段首段引用配置。"""
    default_settings = {
        "default_enabled": True,
        "groups": {}
    }

    try:
        config_data = read_runtime_config()
        data = config_data.get("split_reply_quote", default_settings)
        if not isinstance(data, dict):
            return default_settings

        groups = data.get("groups", {})
        if not isinstance(groups, dict):
            groups = {}

        return {
            "default_enabled": normalize_bool_config(data.get("default_enabled", True), default=True),
            "groups": {str(k): normalize_bool_config(v, default=True) for k, v in groups.items()}
        }
    except Exception as e:
        print(f"加载分段引用配置失败: {e}")
        return default_settings


def is_split_reply_quote_enabled(group_id: int = None) -> bool:
    """检查是否启用“多段回复首段引用触发者”功能。

    优先读取 FeatureSwitches.split_reply_quote。
    若用户仍保留旧版 split_reply_quote 配置，则在缺少新开关时向后兼容。
    """
    config_data = read_runtime_config()
    raw_feature_switches = config_data.get("FeatureSwitches", {}) if isinstance(config_data, dict) else {}
    if isinstance(raw_feature_switches, dict) and "split_reply_quote" in raw_feature_switches:
        return normalize_bool_config(raw_feature_switches.get("split_reply_quote"), default=True)

    legacy_settings = load_split_reply_quote_settings()
    return normalize_bool_config(legacy_settings.get("default_enabled", True), default=True)

def filter_sensitive_content(text: str) -> str:
    if not text:
        return text

    if not is_feature_enabled("sensitive_filter", True):
        return text

    sensitive_words = get_sensitive_words_mapping()
    if not sensitive_words:
        return text

    sorted_keys = sorted(sensitive_words.keys(), key=len, reverse=True)

    for sensitive in sorted_keys:
        replacement = sensitive_words[sensitive]
        pattern = re.compile(re.escape(sensitive), flags=re.IGNORECASE)
        text = pattern.sub(replacement, text)
        
    return text

# ==================== 总结功能限制 ====================
SUMMARY_PER_DAY_LIMIT = int(user_cfg.get("summary_per_day_limit", 1))
SUMMARY_MAX_MESSAGES = int(user_cfg.get("summary_max_messages", 200))
daily_summary_records = defaultdict(lambda: defaultdict(int))  # {group_id: {date: count}}
_summary_inflight_lock = threading.Lock()
_summary_inflight_groups = set()


def can_summary_today(group_id: str) -> tuple[bool, str]:
    """检查群聊今天是否还可以总结"""
    group_id = str(group_id)
    today = date.today().isoformat()
    today_count = daily_summary_records[group_id][today]

    if today_count >= SUMMARY_PER_DAY_LIMIT:
        return False, f"❌ 本群今天已经总结过了，请明天再试 (｡•́︿•̀｡)"

    return True, f"还可以总结，今天已总结 {today_count} 次"


def try_begin_summary(group_id: str) -> tuple[bool, str]:
    """原子检查每日次数并占用本群总结槽。"""
    key = str(group_id)
    with _summary_inflight_lock:
        if key in _summary_inflight_groups:
            return False, "⏳ 本群已有总结任务正在进行，请等待完成后再试"
        can_summary, message = can_summary_today(key)
        if not can_summary:
            return False, message
        _summary_inflight_groups.add(key)
        return True, message


def end_summary(group_id: str) -> None:
    with _summary_inflight_lock:
        _summary_inflight_groups.discard(str(group_id))


def record_summary(group_id: str):
    """记录群聊的一次总结"""
    group_id = str(group_id)
    today = date.today().isoformat()
    daily_summary_records[group_id][today] += 1
    cleanup_old_summary_records()
    save_summary_records()


def cleanup_old_summary_records():
    """清理超过7天的总结记录"""
    try:
        current_date = date.today()
        for group_id in list(daily_summary_records.keys()):
            for record_date in list(daily_summary_records[group_id].keys()):
                try:
                    record_date_obj = date.fromisoformat(record_date)
                    days_diff = (current_date - record_date_obj).days
                    if days_diff > 7:
                        del daily_summary_records[group_id][record_date]
                except ValueError:
                    del daily_summary_records[group_id][record_date]

            if not daily_summary_records[group_id]:
                del daily_summary_records[group_id]
    except Exception as e:
        print(f"清理总结记录时出错: {e}")


def save_summary_records():
    """保存总结记录到文件"""
    try:
        os.makedirs(os.path.join(str(BASE_DIR), "data", 'sum_up'), exist_ok=True)
        records_path = os.path.join(str(BASE_DIR), "data", 'sum_up', 'summary_records.json')

        serializable_records = {}
        for group_id, dates in daily_summary_records.items():
            serializable_records[str(group_id)] = dict(dates)

        atomic_write_json(records_path, serializable_records, indent=2)
    except Exception as e:
        print(f"保存总结记录失败: {e}")


def load_summary_records():
    """从文件加载总结记录"""
    global daily_summary_records
    try:
        records_path = os.path.join(str(BASE_DIR), "data", 'sum_up', 'summary_records.json')
        if os.path.exists(records_path):
            with open(records_path, 'r', encoding='utf-8') as f:
                loaded_records = json.load(f)

            daily_summary_records.clear()
            for group_id, dates in loaded_records.items():
                for record_date, count in dates.items():
                    daily_summary_records[group_id][record_date] = count

            cleanup_old_summary_records()
    except Exception as e:
        print(f"加载总结记录失败: {e}")


# 加载总结记录
load_summary_records()

# 注册退出时的保存函数
atexit.register(save_summary_records)

# ==================== 目录创建 ====================
os.makedirs(os.path.join(str(BASE_DIR), "data", 'sum_up'), exist_ok=True)
os.makedirs(os.path.join(str(BASE_DIR), "data", 'ai_memory'), exist_ok=True)
os.makedirs(os.path.join(str(BASE_DIR), "data", 'compression'), exist_ok=True)
TEMPS_DIR = str(BASE_DIR / "temps")
os.makedirs(TEMPS_DIR, exist_ok=True)
os.makedirs(str(BASE_DIR / "Tools"), exist_ok=True)


def cleanup_old_quote_temps(max_age_seconds: int = 300) -> None:
    """清理过期 quote 临时图，路径基于项目根目录而非 cwd。"""
    try:
        now = time.time()
        if not os.path.isdir(TEMPS_DIR):
            return
        for name in os.listdir(TEMPS_DIR):
            if not name.startswith("quote_") or not name.endswith(".png"):
                continue
            full = os.path.join(TEMPS_DIR, name)
            try:
                if now - os.path.getmtime(full) > max_age_seconds:
                    os.remove(full)
            except Exception:
                pass
    except Exception:
        pass


# ==================== 聊天数据库（兼容旧代码）====================
def default_factory():
    return {
        "history": deque(maxlen=300),  # ponytail: 原1000条约占30MB/群，总结只用200条，300够用
        "token_counter": 0
    }


class GroupKeyedChatDB(defaultdict):
    """群聊数据库：统一群号键为字符串。

    修复：运行时 add_message 用 event.group_id(int)，而从 chat_db.json 加载与
    /reset 用 str(group_id)，导致 int/str 两套键分裂 —— 重启后总结读不到历史，
    落盘时还会互相覆盖丢数据。这里在所有读写入口把键归一化为 str，彻底消除混用。"""

    @staticmethod
    def _norm(key):
        return str(key)

    def __getitem__(self, key):
        return super().__getitem__(self._norm(key))

    def __setitem__(self, key, value):
        super().__setitem__(self._norm(key), value)

    def __contains__(self, key):
        return super().__contains__(self._norm(key))

    def __delitem__(self, key):
        super().__delitem__(self._norm(key))

    def get(self, key, default=None):
        return super().get(self._norm(key), default)


def load_chat_db():
    """加载聊天数据库 - 优先 JSON，回退兼容旧 pickle 文件（一次性迁移）。"""
    chat_db = GroupKeyedChatDB(default_factory)
    json_path = os.path.join(str(BASE_DIR), "data", 'sum_up', 'chat_db.json')
    pkl_path = os.path.join(str(BASE_DIR), "data", 'sum_up', 'chat_db.pkl')

    loaded_db = None
    if os.path.exists(json_path) and os.path.getsize(json_path) > 0:
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                loaded_db = json.load(f)
        except Exception as e:
            print(f"SumUp: 加载 chat_db.json 失败: {e}")
    elif os.path.exists(pkl_path) and os.path.getsize(pkl_path) > 0:
        try:
            with open(pkl_path, 'rb') as f:
                loaded_db = pickle.load(f)
            print("SumUp: 检测到旧 chat_db.pkl，已读取并将迁移为 chat_db.json")
        except Exception as e:
            print(f"SumUp: 加载旧 chat_db.pkl 失败: {e}")

    if isinstance(loaded_db, dict):
        for group_id, data in loaded_db.items():
            if not isinstance(data, dict):
                continue
            history_list = data.get("history", []) or []
            try:
                token_counter = int(data.get("token_counter", 0) or 0)
            except (TypeError, ValueError):
                token_counter = 0
            chat_db[group_id]["history"] = deque(history_list, maxlen=300)
            chat_db[group_id]["token_counter"] = token_counter

    return chat_db


chat_db = load_chat_db()


# ==================== 黑名单功能 ====================
def load_blacklist():
    """兼容旧调用：黑名单统一改为从 config.json 读取。"""
    return load_config_blacklist()


def load_config_blacklist():
    """从config.json加载黑名单"""
    try:
        config_data = read_runtime_config()
        if "black_list" in config_data:
            return set(str(item).strip() for item in config_data["black_list"])
        return set()
    except Exception as e:
        print(f"从config加载黑名单失败: {e}")
        return set()


def load_admin_lists_from_config() -> tuple[list, list]:
    """只从 config.json 读取管理列表，统一为单一管理员等级。"""
    try:
        config_data = read_runtime_config()
    except Exception as e:
        print(f"读取管理列表失败: {e}")
        return [], []

    others = config_data.get("Others", {})
    if not isinstance(others, dict):
        others = {}

    root_users = [str(x).strip() for x in others.get("ROOT_User", []) if str(x).strip()]
    owner_users = [str(x).strip() for x in config_data.get("owner", []) if str(x).strip()]
    admin_users = []
    seen = set()
    for item in owner_users + root_users:
        if item and item not in seen:
            seen.add(item)
            admin_users.append(item)
    return admin_users[:], admin_users[:]


def get_all_blacklist():
    """获取所有黑名单"""
    return load_config_blacklist()


def is_user_blacklisted(user_id: str, blacklist: set) -> bool:
    """检查用户是否在黑名单中"""
    user_id_str = str(user_id)

    if user_id_str in blacklist:
        return True

    for item in blacklist:
        if ',' in item:
            parts = item.split(',')
            if len(parts) >= 1:
                item_id = parts[0].strip()
                if item_id == user_id_str:
                    return True
        elif item == user_id_str:
            return True

    return False


# ==================== 配置读写 ====================
def Read_Settings():
    """从 config.json 读取权限设置。"""
    global ROOT_User, Super_User, Manage_User
    cleanup_legacy_config_files()
    admin_users, _ = load_admin_lists_from_config()
    ROOT_User = admin_users[:]
    Super_User = admin_users[:]
    Manage_User = admin_users[:]


def Write_Settings(s: list, m: list) -> bool:
    """写入权限设置到 config.json。"""
    s = [item for item in s if item]
    m = [item for item in m if item]
    global ROOT_User, Super_User, Manage_User

    try:
        config_data = read_runtime_config()

        manage_users = [str(item).strip() for item in (m or s) if str(item).strip()]
        config_data["owner"] = manage_users
        others = config_data.get("Others", {})
        if not isinstance(others, dict):
            others = {}
            config_data["Others"] = others
        others["ROOT_User"] = manage_users

        if not write_runtime_config(config_data):
            return False

        cleanup_legacy_config_files()

        ROOT_User = manage_users[:]
        Super_User = manage_users[:]
        Manage_User = manage_users[:]
        return True
    except Exception as e:
        print(f"写入 config 管理列表失败: {e}")
        return False


# ==================== 工具函数 ====================


def is_admin_user(user_id) -> bool:
    user_id = str(user_id)
    return user_id in ROOT_User or user_id in Super_User or user_id in Manage_User


def seconds_to_hms(total_seconds):
    """秒转换为时分秒"""
    hours = total_seconds // 3600
    remaining_seconds = total_seconds % 3600
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60
    return f"{hours}h, {minutes}m, {seconds}s"


def has_emoji(s: str) -> bool:
    """检查消息是否“只有一个表情”（支持肤色/组合 emoji）。"""
    text = str(s or "").strip()
    if not text:
        return False
    # 去掉常见空白后，整段应恰好是 1 个 emoji（可含 ZWJ/变体选择符）
    compact = re.sub(r"\s+", "", text)
    return emoji.emoji_count(compact) == 1 and emoji.replace_emoji(compact, replace="") == ""


from bot.estimate import estimate_tokens


_main_psutil_proc = None

def get_system_info():
    """获取系统信息"""
    global _main_psutil_proc
    version_info = platform.platform()
    architecture = platform.architecture()

    if _main_psutil_proc is None:
        _main_psutil_proc = psutil.Process(os.getpid())
        _main_psutil_proc.cpu_percent(interval=None)
    cpu_usage = _main_psutil_proc.cpu_percent(interval=None)

    virtual_memory = psutil.virtual_memory()
    memory_usage_percentage = virtual_memory.percent

    try:
        import GPUtil
        gpus = GPUtil.getGPUs()
    except Exception:
        gpus = []
    gpu_usage = [gpu.load for gpu in gpus] if gpus else []

    return {
        "version_info": version_info,
        "architecture": architecture,
        "cpu_usage": cpu_usage,
        "memory_usage_percentage": memory_usage_percentage,
        "gpu_usage": gpu_usage,
    }


def extract_plain_text_from_message(message) -> str:
    parts = []
    try:
        for segment in message:
            if isinstance(segment, Segments.Text):
                parts.append(segment.text)
    except Exception:
        return ""
    return "".join(parts).strip()


def _short_text(text, limit: int = 60) -> str:
    text = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _safe_sender_name(name, fallback: str = "未知") -> str:
    return _short_text(filter_sensitive_content(str(name or fallback)), 24)


def _message_preview(message, limit: int = 60) -> str:
    try:
        if isinstance(message, str):
            return _short_text(filter_sensitive_content(message), limit)
        text = extract_plain_text_from_message(message)
        if text:
            return _short_text(filter_sensitive_content(text), limit)
        raw = str(message)
        raw = raw.replace("[", "<").replace("]", ">")
        return _short_text(filter_sensitive_content(raw), limit)
    except Exception:
        return "[消息]"


def log_console(tag: str, content: str):
    print(f"[{tag}] {_short_text(content, 180)}")


def log_receive_private(user_id, nickname: str, message):
    log_console("RECV", f"私聊 {user_id}({_safe_sender_name(nickname)}) {_message_preview(message)}")


def log_receive_group(group_id, user_id, nickname: str, message):
    log_console("RECV", f"群 {group_id} {user_id}({_safe_sender_name(nickname)}) {_message_preview(message)}")


def log_api_request(scene: str, model: str, base_url: str, current_key: str, message_count: int, preview: str):
    host = urllib.parse.urlparse(base_url).netloc or base_url
    key_mask = (current_key[:6] + "...") if current_key else "none"
    log_console("API", f"{scene} -> {model} @{host} key={key_mask} msg={message_count} q={_short_text(preview, 50)}")


def log_api_success(scene: str, model: str, total_tokens: int, reply: str):
    log_console("API", f"{scene} <- {model} ok tokens={total_tokens} a={_short_text(reply, 50)}")


def log_api_failure(scene: str, model: str, current_key: str, error):
    key_mask = (current_key[:6] + "...") if current_key else "none"
    log_console("API", f"{scene} xx {model} key={key_mask} err={_short_text(error, 90)}")


def ensure_llm_reply_passes_failover_check(reply_text: str):
    """当回复命中配置关键词时，抛出异常触发自动切换下一个 API。"""
    keyword = find_llm_reply_failover_keyword(reply_text)
    if not keyword:
        return

    raise Exception(f"LLM 回复命中切换关键词: {keyword}")


class LoggedActions:
    def __init__(self, actions):
        self._actions = actions

    def __getattr__(self, item):
        return getattr(self._actions, item)

    async def send(self, *args, **kwargs):
        group_id = kwargs.get("group_id")
        user_id = kwargs.get("user_id")
        message = kwargs.get("message")
        target = f"群 {group_id}" if group_id else f"私聊 {user_id}"
        log_console("SEND", f"{target} {_message_preview(message)}")
        return await self._actions.send(*args, **kwargs)

    async def send_group_forward_msg(self, *args, **kwargs):
        group_id = kwargs.get("group_id")
        log_console("SEND", f"群 {group_id} [转发消息]")
        return await self._actions.send_group_forward_msg(*args, **kwargs)

    async def del_message(self, *args, **kwargs):
        msg_id = args[0] if args else kwargs.get("message_id")
        log_console("SEND", f"撤回 msg={msg_id}")
        return await self._actions.del_message(*args, **kwargs)


class QQHttpSendError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def clear_current_qq_actions():
    """断开/失败/停止时清理出站 Actions 引用。"""
    global _current_qq_actions, _current_bot_self_id
    with _qq_actions_lock:
        _current_qq_actions = None
        _current_bot_self_id = None


def _qq_http_error(code: str, message: str, status: int = 400) -> dict:
    return {"ok": False, "code": code, "error": message, "status": status}


def _parse_positive_id(value, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise QQHttpSendError("INVALID_TARGET", f"{field_name} 必须是正整数")
    if isinstance(value, int):
        target = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.isdigit():
            raise QQHttpSendError("INVALID_TARGET", f"{field_name} 必须是正整数")
        target = int(raw)
    else:
        raise QQHttpSendError("INVALID_TARGET", f"{field_name} 必须是正整数")
    if target <= 0:
        raise QQHttpSendError("INVALID_TARGET", f"{field_name} 必须大于 0")
    return target


def _parse_qq_http_target(payload: dict):
    has_target_type = "target_type" in payload
    has_target_id = "target_id" in payload
    has_user = "user_id" in payload
    has_group = "group_id" in payload

    if has_target_type or has_target_id:
        if not (has_target_type and has_target_id):
            raise QQHttpSendError("INVALID_TARGET", "target_type 与 target_id 必须同时提供")
        if has_user or has_group:
            raise QQHttpSendError("AMBIGUOUS_TARGET", "请只使用 target_type/target_id，或只使用 user_id/group_id")
        target_type = str(payload.get("target_type") or "").strip().lower()
        if target_type not in {"private", "group"}:
            raise QQHttpSendError("INVALID_TARGET", "target_type 必须是 private 或 group")
        return target_type, _parse_positive_id(payload.get("target_id"), "target_id")

    if has_user and has_group:
        raise QQHttpSendError("AMBIGUOUS_TARGET", "user_id 与 group_id 只能提供一个")
    if has_user:
        return "private", _parse_positive_id(payload.get("user_id"), "user_id")
    if has_group:
        return "group", _parse_positive_id(payload.get("group_id"), "group_id")
    raise QQHttpSendError("INVALID_TARGET", "缺少目标：需要 target_type/target_id 或 user_id/group_id")


def _require_only_keys(data: dict, allowed: set, segment_type: str):
    extra = set(data.keys()) - allowed
    if extra:
        raise QQHttpSendError(
            "INVALID_SEGMENT",
            f"{segment_type} 段包含不支持字段: {', '.join(sorted(extra))}",
        )


def _validate_http_url(value: str, field_name: str = "file") -> str:
    raw = str(value or "").strip()
    if not raw:
        raise QQHttpSendError("INVALID_MEDIA", f"{field_name} 不能为空")
    if len(raw) > 4096:
        raise QQHttpSendError("INVALID_MEDIA", f"{field_name} 过长")
    lower = raw.lower()
    if lower.startswith(("file:", "data:", "ftp:", "\\\\")):
        raise QQHttpSendError("FORBIDDEN_MEDIA", f"{field_name} 不允许本地文件或非 HTTP 来源", 403)
    if not lower.startswith(("http://", "https://")):
        raise QQHttpSendError("FORBIDDEN_MEDIA", f"{field_name} 仅允许 http/https URL", 403)
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        raise QQHttpSendError("INVALID_MEDIA", f"{field_name} 不是合法 URL")
    if parsed.username or parsed.password:
        raise QQHttpSendError("FORBIDDEN_MEDIA", f"{field_name} 不允许带用户名/密码的 URL", 403)
    if not parsed.netloc:
        raise QQHttpSendError("INVALID_MEDIA", f"{field_name} 缺少主机名")
    return raw


def _validate_image_source(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise QQHttpSendError("INVALID_MEDIA", "image.file 不能为空")
    lower = raw.lower()
    if lower.startswith("base64://"):
        # 先剥掉所有空白：从文件或表单读出的 base64 常带换行，
        # validate=True 会把换行当非法字符，直接判成"无法解码"。
        b64 = re.sub(r"\s+", "", raw[9:])
        if not b64:
            raise QQHttpSendError("INVALID_MEDIA", "base64 图片内容为空")
        if len(b64) > 4 * 1024 * 1024:
            raise QQHttpSendError("PAYLOAD_TOO_LARGE", "base64 图片过大", 413)
        try:
            padding = "=" * ((4 - len(b64) % 4) % 4)
            decoded = base64.b64decode(b64 + padding, validate=True)
        except Exception:
            raise QQHttpSendError("INVALID_MEDIA", "base64 图片无法解码")
        if len(decoded) > 3 * 1024 * 1024:
            raise QQHttpSendError("PAYLOAD_TOO_LARGE", "解码后图片超过 3MB", 413)
        # 回传剥离空白后的值，避免把换行透传给 OneBot 实现
        return f"base64://{b64}"
    if lower.startswith(("file:", "data:", "ftp:")) or "\\" in raw or (len(raw) >= 2 and raw[1] == ":"):
        raise QQHttpSendError("FORBIDDEN_MEDIA", "image 不允许本地文件路径", 403)
    return _validate_http_url(raw, "image.file")


def _build_one_qq_http_segment(item: dict, text_total: list):
    if not isinstance(item, dict):
        raise QQHttpSendError("INVALID_SEGMENT", "每个消息段必须是对象")
    seg_type = str(item.get("type") or "").strip().lower()
    data = item.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise QQHttpSendError("INVALID_SEGMENT", f"{seg_type or 'unknown'} 段的 data 必须是对象")

    if seg_type == "text":
        _require_only_keys(data, {"text"}, "text")
        text_value = data.get("text", "")
        if not isinstance(text_value, str):
            raise QQHttpSendError("INVALID_SEGMENT", "text 段的 text 必须是字符串")
        if not text_value:
            raise QQHttpSendError("EMPTY_MESSAGE", "text 段不能为空")
        if len(text_value) > 10000:
            raise QQHttpSendError("INVALID_SEGMENT", "单个 text 段最多 10000 字符")
        text_total[0] += len(text_value)
        if text_total[0] > 20000:
            raise QQHttpSendError("INVALID_SEGMENT", "全部 text 合计最多 20000 字符")
        return Segments.Text(text_value)

    if seg_type == "image":
        _require_only_keys(data, {"file", "url", "summary"}, "image")
        file_value = data.get("file") or data.get("url")
        if not file_value:
            raise QQHttpSendError("INVALID_SEGMENT", "image 段需要 file 或 url")
        source = _validate_image_source(str(file_value))
        summary = data.get("summary")
        if summary is None:
            return Segments.Image(file=source)
        if not isinstance(summary, str):
            raise QQHttpSendError("INVALID_SEGMENT", "image.summary 必须是字符串")
        return Segments.Image(file=source, summary=summary[:64] or "[图片]")

    if seg_type == "at":
        _require_only_keys(data, {"qq"}, "at")
        qq = data.get("qq")
        if qq is None:
            raise QQHttpSendError("INVALID_SEGMENT", "at 段需要 qq")
        if isinstance(qq, bool):
            raise QQHttpSendError("INVALID_SEGMENT", "at.qq 非法")
        qq_text = str(qq).strip()
        if not qq_text or (qq_text != "all" and not qq_text.isdigit()):
            raise QQHttpSendError("INVALID_SEGMENT", "at.qq 必须是 QQ 号或 all")
        return Segments.At(qq=qq_text)

    if seg_type == "reply":
        _require_only_keys(data, {"id"}, "reply")
        msg_id = data.get("id")
        if msg_id is None or isinstance(msg_id, bool):
            raise QQHttpSendError("INVALID_SEGMENT", "reply 段需要 id")
        msg_id_text = str(msg_id).strip()
        if not msg_id_text:
            raise QQHttpSendError("INVALID_SEGMENT", "reply.id 不能为空")
        return Segments.Reply(id=msg_id_text)

    if seg_type == "face":
        _require_only_keys(data, {"id"}, "face")
        face_id = data.get("id")
        if face_id is None or isinstance(face_id, bool):
            raise QQHttpSendError("INVALID_SEGMENT", "face 段需要 id")
        face_text = str(face_id).strip()
        if not face_text:
            raise QQHttpSendError("INVALID_SEGMENT", "face.id 不能为空")
        return Segments.Faces(id=face_text)

    if seg_type == "record":
        _require_only_keys(data, {"file", "url"}, "record")
        file_value = data.get("file") or data.get("url")
        if not file_value:
            raise QQHttpSendError("INVALID_SEGMENT", "record 段需要 file 或 url")
        return Segments.Record(file=_validate_http_url(str(file_value), "record.file"))

    if seg_type == "video":
        _require_only_keys(data, {"file", "url"}, "video")
        file_value = data.get("file") or data.get("url")
        if not file_value:
            raise QQHttpSendError("INVALID_SEGMENT", "video 段需要 file 或 url")
        return Segments.Video(file=_validate_http_url(str(file_value), "video.file"))

    if seg_type == "location":
        _require_only_keys(data, {"lat", "lon"}, "location")
        lat = data.get("lat")
        lon = data.get("lon")
        if lat is None or lon is None or isinstance(lat, bool) or isinstance(lon, bool):
            raise QQHttpSendError("INVALID_SEGMENT", "location 段需要 lat 与 lon")
        return Segments.Location(lat=str(lat), lon=str(lon))

    if seg_type == "contact":
        _require_only_keys(data, {"type", "id"}, "contact")
        contact_type = str(data.get("type") or "").strip().lower()
        contact_id = data.get("id")
        if contact_type not in {"qq", "group"}:
            raise QQHttpSendError("INVALID_SEGMENT", "contact.type 必须是 qq 或 group")
        if contact_id is None or isinstance(contact_id, bool):
            raise QQHttpSendError("INVALID_SEGMENT", "contact 段需要 id")
        contact_id_text = str(contact_id).strip()
        if not contact_id_text.isdigit():
            raise QQHttpSendError("INVALID_SEGMENT", "contact.id 必须是数字")
        return Segments.Contact(type=contact_type, id=contact_id_text)

    if seg_type == "json":
        _require_only_keys(data, {"data"}, "json")
        raw = data.get("data")
        if raw is None:
            raise QQHttpSendError("INVALID_SEGMENT", "json 段需要 data")
        if isinstance(raw, (dict, list)):
            encoded = json.dumps(raw, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > 64 * 1024:
                raise QQHttpSendError("INVALID_SEGMENT", "json 卡片最大 64KB")
            return Segments.Json(data=raw)
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > 64 * 1024:
                raise QQHttpSendError("INVALID_SEGMENT", "json 卡片最大 64KB")
            return Segments.Json(data=raw)
        raise QQHttpSendError("INVALID_SEGMENT", "json.data 必须是对象、数组或字符串")

    if seg_type == "dice":
        _require_only_keys(data, set(), "dice")
        return Segments.Dice()

    if seg_type == "rps":
        _require_only_keys(data, set(), "rps")
        return Segments.Rps()

    if seg_type == "music":
        _require_only_keys(data, {"type", "id", "url", "audio", "title"}, "music")
        music_type = str(data.get("type") or "").strip()
        if not music_type:
            raise QQHttpSendError("INVALID_SEGMENT", "music 段需要 type")
        kwargs = {"type": music_type}
        if "id" in data and data.get("id") is not None:
            kwargs["id"] = str(data.get("id"))
        if "url" in data and data.get("url") is not None:
            kwargs["url"] = _validate_http_url(str(data.get("url")), "music.url")
        if "audio" in data and data.get("audio") is not None:
            kwargs["audio"] = _validate_http_url(str(data.get("audio")), "music.audio")
        if "title" in data and data.get("title") is not None:
            if not isinstance(data.get("title"), str):
                raise QQHttpSendError("INVALID_SEGMENT", "music.title 必须是字符串")
            kwargs["title"] = data.get("title")
        if music_type == "custom" and not kwargs.get("url"):
            raise QQHttpSendError("INVALID_SEGMENT", "music.custom 需要 url")
        if music_type != "custom" and not kwargs.get("id"):
            raise QQHttpSendError("INVALID_SEGMENT", "music 平台分享需要 id")
        return Segments.Music(**kwargs)

    raise QQHttpSendError(
        "UNSUPPORTED_SEGMENT",
        f"不支持的消息段类型: {seg_type or 'unknown'}",
        403,
    )


def build_qq_http_segments(payload: dict) -> list:
    """把 HTTP 请求体归一化为 Hyper Segments 列表。"""
    present = [key for key in ("text", "message", "segments") if key in payload]
    if len(present) != 1:
        raise QQHttpSendError(
            "AMBIGUOUS_MESSAGE",
            "请只提供 text、message、segments 三者之一",
        )

    key = present[0]
    value = payload.get(key)
    text_total = [0]

    if key == "text":
        if not isinstance(value, str):
            raise QQHttpSendError("INVALID_MESSAGE", "text 必须是字符串")
        if not value:
            raise QQHttpSendError("EMPTY_MESSAGE", "text 不能为空")
        if len(value) > 10000:
            raise QQHttpSendError("INVALID_MESSAGE", "text 最多 10000 字符")
        return [Segments.Text(value)]

    if key == "message" and isinstance(value, str):
        if not value:
            raise QQHttpSendError("EMPTY_MESSAGE", "message 不能为空")
        if len(value) > 10000:
            raise QQHttpSendError("INVALID_MESSAGE", "message 最多 10000 字符")
        return [Segments.Text(value)]

    if key in {"message", "segments"}:
        if not isinstance(value, list):
            raise QQHttpSendError("INVALID_MESSAGE", f"{key} 必须是消息段数组或字符串")
        if not value:
            raise QQHttpSendError("EMPTY_MESSAGE", f"{key} 不能为空数组")
        if len(value) > 50:
            raise QQHttpSendError("INVALID_MESSAGE", "消息段最多 50 个")
        built = []
        for item in value:
            seg = _build_one_qq_http_segment(item, text_total)
            if not callable(getattr(seg, "to_json", None)):
                raise QQHttpSendError("INTERNAL_ERROR", "消息段构建失败", 500)
            built.append(seg)
        return built

    raise QQHttpSendError("INVALID_MESSAGE", "无法解析消息内容")


def _extract_send_message_id(ret):
    try:
        data = getattr(ret, "data", None)
        if data is None:
            return None
        raw = getattr(data, "raw", None)
        if isinstance(raw, dict) and "message_id" in raw:
            return raw.get("message_id")
        if hasattr(data, "message_id"):
            return getattr(data, "message_id")
        if isinstance(data, dict):
            return data.get("message_id")
    except Exception:
        return None
    return None


def send_qq_message_from_http(payload: dict, _bypass_feature_switch: bool = False) -> dict:
    """供 WebUI 线程调用的同步 QQ 发送入口。"""
    if not _bypass_feature_switch and not is_feature_enabled("http_send_api", True):
        return _qq_http_error(
            "FEATURE_DISABLED",
            "HTTP 消息发送接口已禁用，请在 WebUI 功能配置中开启",
            403,
        )

    if not isinstance(payload, dict):
        return _qq_http_error("INVALID_PAYLOAD", "请求体必须是 JSON 对象", 400)

    try:
        target_type, target_id = _parse_qq_http_target(payload)
        segments = build_qq_http_segments(payload)
    except QQHttpSendError as e:
        return _qq_http_error(e.code, e.message, e.status)
    except Exception as e:
        print(f"[HTTP SEND] 参数解析失败: {e}")
        return _qq_http_error("INVALID_PAYLOAD", "请求参数无效", 400)

    with _qq_actions_lock:
        actions = _current_qq_actions
    if actions is None:
        return _qq_http_error("BOT_NOT_CONNECTED", "机器人尚未连接或连接已断开", 503)

    message = Manager.Message(*segments)
    send_kwargs = {"message": message}
    if target_type == "group":
        send_kwargs["group_id"] = int(target_id)
    else:
        send_kwargs["user_id"] = int(target_id)

    if not _qq_send_lock.acquire(timeout=3):
        return _qq_http_error("SEND_BUSY", "发送通道繁忙，请稍后重试", 429)

    try:
        try:
            ret = asyncio.run(actions.send(**send_kwargs))
        except TimeoutError:
            return _qq_http_error(
                "SEND_TIMEOUT",
                "等待发送回执超时；消息可能已发出，请勿无条件重试",
                504,
            )
        except Exception as e:
            err = str(e)
            low = err.lower()
            if any(x in low for x in ["not connected", "closed", "连接", "socket", "timeout"]):
                clear_current_qq_actions()
                return _qq_http_error("BOT_NOT_CONNECTED", f"发送失败，连接不可用: {err}", 503)
            print(f"[HTTP SEND] 发送失败: {e}")
            traceback.print_exc()
            return _qq_http_error("SEND_FAILED", f"发送失败: {err}", 502)

        message_id = _extract_send_message_id(ret)
        data = {
            "target_type": target_type,
            "target_id": str(target_id),
            "segment_count": len(segments),
        }
        if message_id is not None:
            data["message_id"] = message_id
        return {
            "ok": True,
            "message": "消息已发送",
            "data": data,
            "status": 200,
        }
    finally:
        try:
            _qq_send_lock.release()
        except Exception:
            pass


def _resolve_bot_self_id() -> Optional[int]:
    """取机器人自己的 QQ 号：优先事件里记录的 self_id，其次配置 uin。"""
    with _qq_actions_lock:
        self_id = _current_bot_self_id
    if self_id:
        try:
            value = int(self_id)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    try:
        uin = int(read_runtime_config().get("uin", 0) or 0)
        return uin if uin > 0 else None
    except (TypeError, ValueError):
        return None


def send_debug_self_message(payload: dict) -> dict:
    """调试用：给机器人自己的 QQ 发一条私聊消息，验证出站链路是否通。

    走和 /api/send 相同的发送实现，但目标固定为机器人自身，
    因此不受 http_send_api 开关限制，也无法用来给任意用户发消息。
    """
    if not isinstance(payload, dict):
        payload = {}

    self_id = _resolve_bot_self_id()
    if not self_id:
        return _qq_http_error(
            "SELF_ID_UNKNOWN",
            "尚未获知机器人 QQ 号。请等待 OneBot 连接建立后重试，或在配置中填写 uin",
            503,
        )

    raw_text = payload.get("text", "")
    text = str(raw_text or "").strip()
    if not text:
        text = f"[调试自检] {bot_name} 出站链路正常 · {time.strftime('%Y-%m-%d %H:%M:%S')}"
    if len(text) > 2000:
        return _qq_http_error("INVALID_MESSAGE", "调试消息最多 2000 字符", 400)

    result = send_qq_message_from_http(
        {"user_id": self_id, "text": text},
        _bypass_feature_switch=True,
    )
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        result["data"]["self_id"] = str(self_id)
        result["data"]["sent_text"] = text
    return result


def render_group_join_welcome_text(user_id, group_id, user_nickname: str = "") -> str:
    """渲染入群欢迎正文。

    只对四个已知占位符做字面替换，不用 str.format，
    这样用户文案里出现 JSON、代码或未成对花括号都不会抛异常。
    """
    raw = get_runtime_others().get("group_join_welcome_text", None)
    text = raw if isinstance(raw, str) else ""
    if not text.strip():
        text = DEFAULT_GROUP_JOIN_WELCOME_TEXT

    replacements = {
        "{bot_name}": str(bot_name),
        "{user_nickname}": str(user_nickname or f"用户{user_id}"),
        "{user_id}": str(user_id),
        "{group_id}": str(group_id if group_id is not None else ""),
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text


def is_at_bot_message(event) -> bool:
    try:
        for segment in event.message:
            if isinstance(segment, Segments.At) and str(segment.qq) == str(event.self_id):
                return True
    except Exception:
        return False
    return False


def is_group_dialog_trigger_for_weak_blacklist(event, user_message: str) -> bool:
    text = (user_message or "").strip()
    plain_text = extract_plain_text_from_message(getattr(event, "message", []))

    if text.startswith(reminder) or plain_text.startswith(reminder):
        return True
    if is_at_bot_message(event):
        return True
    if any(trigger in plain_text for trigger in ROBOT_NAME_TRIGGERS):
        return True
    return False


def should_block_by_weak_blacklist(event, user_id=None, user_message: str = "", is_group=False) -> bool:
    if not is_group or not is_feature_enabled("weak_blacklist", True):
        return False

    others = get_runtime_others()
    weak_users = {str(user).strip() for user in others.get("weak_blacklist_users", []) if str(user).strip()}
    if str(user_id) not in weak_users:
        return False

    if not is_group_dialog_trigger_for_weak_blacklist(event, user_message):
        return False

    probability = normalize_probability_config(
        others.get("weak_blacklist_trigger_probability", 0.3),
        default=0.3,
    )

    if random.random() <= probability:
        print(f"弱黑名单放行: user_id={user_id}, probability={probability}")
        return False

    print(f"弱黑名单拦截: user_id={user_id}, probability={probability}")
    return True


def should_trigger_random_group_chat(user_message: str = "") -> bool:
    """按配置概率让机器人在普通群消息下主动参与对话。"""
    if not is_feature_enabled("group_chat", True):
        return False

    text = str(user_message or "").strip()
    if not text:
        return False

    probability = normalize_probability_config(
        get_runtime_others().get("group_random_reply_probability", user_cfg.get("group_random_reply_probability", 0)),
        default=0.0,
    )
    if probability <= 0:
        return False

    triggered = random.random() <= probability
    if triggered:
        print(f"群聊概率触发放行: probability={probability}, text={_short_text(text, 40)}")
    return triggered


def is_group_random_reply_quote_enabled() -> bool:
    return normalize_bool_config(
        get_runtime_others().get("group_random_reply_quote", user_cfg.get("group_random_reply_quote", False)),
        default=False,
    )


# ==================== 重启状态持久化工具 ====================
RESTART_STATE_FILE = "restart.temp"


def save_restart_state(target_type: str, target_id: int) -> bool:
    """保存重启后通知目标，target_type: group/private"""
    try:
        data = {
            "type": str(target_type),
            "id": int(target_id),
            "time": time.time()
        }
        atomic_write_json(RESTART_STATE_FILE, data, indent=None)
        return True
    except Exception as e:
        print(f"保存重启状态失败: {e}")
        return False


def load_restart_state() -> Optional[dict]:
    """读取重启状态"""
    try:
        if not os.path.exists(RESTART_STATE_FILE):
            return None
        with open(RESTART_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"读取重启状态失败: {e}")
        return None


def clear_restart_state():
    """清除重启状态文件"""
    try:
        if os.path.exists(RESTART_STATE_FILE):
            os.remove(RESTART_STATE_FILE)
    except Exception as e:
        print(f"清除重启状态失败: {e}")
def format_exception_for_user(e: Exception) -> str:
    """将异常格式化为适合发送给用户的文本"""
    try:
        parts = []

        status_code = getattr(e, "status_code", None)
        if status_code:
            parts.append(f"状态码: {status_code}")

        body = getattr(e, "body", None)
        if body:
            try:
                if isinstance(body, (dict, list)):
                    body_text = json.dumps(body, ensure_ascii=False)
                else:
                    body_text = str(body)
                parts.append(f"响应体: {body_text}")
            except Exception:
                parts.append(f"响应体: {str(body)}")

        raw = str(e).strip()
        if raw:
            parts.append(f"异常信息: {raw}")

        if not parts:
            parts.append(f"异常信息: {repr(e)}")

        msg = "\n".join(parts)

        if len(msg) > 1000:
            msg = msg[:1000] + "\n...(错误信息过长，已截断)"

        return msg
    except Exception:
        return f"发生异常：{str(e)}"



def build_user_error_text(error: Exception, error_type: str = "program") -> str:
    """按统一格式生成发送给用户的错误文本。"""
    error_msg = filter_sensitive_content(format_exception_for_user(error))
    if error_type == "ai":
        return f"XcBot请求失败。\n{error_msg}"
    return f"XcBot出现错误\n{error_msg}"


async def send_error_detail(actions, event, error: Exception, is_group: bool, reply: bool = True, error_type: str = "program"):
    """向用户发送具体错误信息"""
    error_msg = build_user_error_text(error, error_type=error_type)

    try:
        if is_group:
            if reply and hasattr(event, "message_id"):
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(
                        Segments.Reply(event.message_id),
                        Segments.Text(error_msg)
                    )
                )
            else:
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(Segments.Text(error_msg))
                )
        else:
            await actions.send(
                user_id=event.user_id,
                message=Manager.Message(Segments.Text(error_msg))
            )
    except Exception as send_err:
        print(f"发送错误详情失败: {send_err} | 原始错误: {error}")


API_REQUEST_TIMEOUT_SECONDS = int(user_cfg.get("api_request_timeout_seconds", 60))


# ==================== 用户昵称获取函数 ====================
nickname_cache = OrderedDict()
MAX_NICKNAME_CACHE = 1000  # ponytail: 5000条昵称约占50MB，1000条够用

async def get_nickname_by_userid(user_id, Manager, actions, group_id: int = None, event=None):
    """通过用户ID获取昵称"""
    global nickname_cache
    cache_key = f"{group_id}_{user_id}" if group_id else f"0_{user_id}"

    if event:
        try:
            sender = getattr(event, 'sender', None)
            if not sender and hasattr(event, 'raw'):
                sender = event.raw.get('sender', {})

            if sender:
                name = (getattr(sender, 'card', '') or getattr(sender, 'nickname', '')) if not isinstance(sender, dict) \
                       else (sender.get('card') or sender.get('nickname'))

                if name:
                    filtered_name = filter_sensitive_content(name)
                    nickname_cache[cache_key] = (filtered_name, time.time())
                    nickname_cache.move_to_end(cache_key)
                    if len(nickname_cache) > MAX_NICKNAME_CACHE:
                        nickname_cache.popitem(last=False)
                    return filtered_name
        except:
            pass

    if cache_key in nickname_cache:
        name, timestamp = nickname_cache[cache_key]
        if time.time() - timestamp < 600:
            nickname_cache.move_to_end(cache_key)
            return name

    try:
        if group_id:
            try:
                member_info = await asyncio.wait_for(
                    actions.get_group_member_info(group_id=group_id, user_id=user_id),
                    timeout=2.0
                )
                nickname = member_info.data.raw.get('card', '') or member_info.data.raw.get('nickname', '')
                if nickname:
                    res = filter_sensitive_content(nickname)
                    nickname_cache[cache_key] = (res, time.time())
                    nickname_cache.move_to_end(cache_key)
                    if len(nickname_cache) > MAX_NICKNAME_CACHE:
                        nickname_cache.popitem(last=False)
                    return res
            except:
                pass

        user_info = await asyncio.wait_for(actions.get_stranger_info(user_id), timeout=2.0)
        nickname = user_info.data.raw.get('nickname', str(user_id))
        res = filter_sensitive_content(nickname)
        nickname_cache[cache_key] = (res, time.time())
        nickname_cache.move_to_end(cache_key)
        if len(nickname_cache) > MAX_NICKNAME_CACHE:
            nickname_cache.popitem(last=False)
        return res
    except:
        return str(user_id)


class LimitedDeepSeekContext:
    """严格限制上下文消息数量的 DeepSeek 上下文 - 系统提示词独立存储"""

    _client_pool: dict = {}  # 类级别共享，所有实例复用同一组 OpenAI client

    def __init__(self, system_prompt: str):
        self.system_prompt = filter_sensitive_content(system_prompt)
        self.max_messages = int(user_cfg.get("context_max_messages", 60))
        self.history = []       # 这里只存 user/assistant 类型历史，不存系统提示词

    def _get_client(self, base_url: str, api_key: str, timeout_seconds: int = None):
        """获取或创建 OpenAI 客户端（支持不同端点）"""
        cache_key = f"{base_url}_{api_key}"
        if cache_key not in self._client_pool:
            client_timeout = int(timeout_seconds or API_REQUEST_TIMEOUT_SECONDS) + 5
            self._client_pool[cache_key] = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=client_timeout,
                max_retries=1
            )
        return self._client_pool[cache_key]

    def _close_clients(self):
        """关闭本实例使用的客户端连接。

        注意：_client_pool 是类级共享池，clear 时不能把整个池关掉，
        否则其他会话 in-flight 请求会被误伤。这里只 close 后清空引用，
        保留池内对象供其他实例继续复用；真正全量关闭见 close_runtime_llm_clients。
        """
        # 不 clear 类池，避免并发会话互相踢连接
        return

    def _extract_text_from_message(self, message) -> str:
        """统一的消息文本提取方法"""
        if isinstance(message, str):
            return message

        try:
            if hasattr(message, 'parts') and message.parts:
                text_parts = []
                for part in message.parts:
                    if hasattr(part, 'text'):
                        text_parts.append(part.text)
                    elif hasattr(part, 'content'):
                        text_parts.append(part.content)
                    elif isinstance(part, str):
                        text_parts.append(part)
                    else:
                        part_str = str(part)
                        if 'object at' not in part_str:
                            text_parts.append(part_str)
                if text_parts:
                    return " ".join(text_parts)

            if hasattr(message, 'content'):
                content = message.content
                if hasattr(content, 'text'):
                    return content.text
                return str(content)

            if hasattr(message, 'text'):
                return message.text

            raw = str(message)
            if 'object at' in raw:
                return "[用户消息]"
            return raw

        except Exception:
            return "[用户消息]"

    def _build_messages(self, current_message=None):
        """
        构建完整消息列表：
        1. system_prompt 永远只作为唯一 system 消息
        2. history 仅包含 user / assistant 消息
        """
        messages = [{"role": "system", "content": build_llm_system_prompt(self.system_prompt)}]

        for msg in self.history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role not in ("user", "assistant"):
                role = "assistant"
            messages.append({
                "role": role,
                "content": content
            })

        if current_message is not None:
            text_content = self._extract_text_from_message(current_message)
            messages.append({"role": "user", "content": text_content})

        return messages

    @staticmethod
    def _clean_content(content):
        """将消息内容中的 base64 图片数据替换为占位符，防止内存膨胀。"""
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            return " ".join(parts) if parts else ""
        if isinstance(content, str) and "data:image/" in content:
            return re.sub(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', '[图片]', content)
        return content

    def _enforce_message_limit(self):
        """强制执行消息数量限制，仅裁剪普通历史，并清理 base64 图片数据"""
        try:
            for msg in self.history:
                content = msg.get("content")
                if content is not None:
                    cleaned = self._clean_content(content)
                    if cleaned is not content:
                        msg["content"] = cleaned
            if len(self.history) > self.max_messages:
                self.history = self.history[-self.max_messages:]
        except Exception:
            pass

    async def agen_content(self, message) -> tuple[str, int, int, int]:
        max_retries = key_manager.get_attempt_count() or 1
        last_error = None
        tried_keys = set()

        for attempt in range(max_retries):
            has_image_message = isinstance(message, dict) and bool(message.get("image_urls"))
            direct_image_mode = has_image_message and get_multimodal_image_mode() == "direct"
            if direct_image_mode:
                current = key_manager.get_next_multimodal_for_request(
                    tried_keys=tried_keys,
                    include_cooldown=True,
                    preferred_model=get_configured_multimodal_model(),
                )
                if not current:
                    current = key_manager.get_next_for_request(
                        tried_keys=tried_keys,
                        include_cooldown=True,
                        require_multimodal=False,
                    )
            else:
                use_multimodal_directly = has_image_message and bool(key_manager.is_default_multimodal())
                require_multimodal = use_multimodal_directly
                current = key_manager.get_next_for_request(
                    tried_keys=tried_keys,
                    include_cooldown=True,
                    require_multimodal=require_multimodal,
                )
            if not current:
                break

            base_url, current_key, model, supports_multimodal, timeout_seconds, display_model = current
            tried_keys.add(key_manager.make_attempt_identity(base_url, current_key, model))

            try:
                self._enforce_message_limit()
                image_urls = []
                relay_total_tokens = 0
                relay_prompt_tokens = 0
                relay_completion_tokens = 0
                if isinstance(message, dict):
                    user_content = str(message.get("text", "") or "")
                    raw_image_urls = message.get("image_urls", []) or []
                    if raw_image_urls and not supports_multimodal:
                        if direct_image_mode:
                            user_content = merge_image_relay_into_user_content(user_content, "当前没有可用的多模态模型直接处理图片，图片内容未被读取。")
                        else:
                            image_description, relay_total_tokens, relay_prompt_tokens, relay_completion_tokens = await relay_images_with_multimodal_model(
                                self,
                                user_content,
                                raw_image_urls,
                            )
                            user_content = merge_image_relay_into_user_content(user_content, image_description)
                    else:
                        image_urls = await prepare_image_inputs_for_model(
                            raw_image_urls,
                            supports_multimodal,
                        )
                    messages = self._build_messages()
                    messages.append({
                        "role": "user",
                        "content": build_openai_message_content(
                            build_llm_user_message(user_content),
                            image_urls=image_urls,
                            supports_multimodal=supports_multimodal,
                        )
                    })
                else:
                    user_content = self._extract_text_from_message(message)
                    messages = self._build_messages(build_llm_user_message(user_content))

                client = self._get_client(base_url, current_key, timeout_seconds)

                # 根据 _enforce_message_limit 的逻辑，由于需要保证 history 里存放内容，通常这里的调用是通过 get_context 的对应 scene 获取的。
                # 既然是 LimitedDeepSeekContext 内部，我们可以用 getattr 获取绑定的 session_id。
                scene = getattr(self, "session_id", "AI")
                
                log_api_request(
                    scene=scene,
                    model=display_model,
                    base_url=base_url,
                    current_key=current_key,
                    message_count=len(messages),
                    preview=user_content
                )

                try:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.chat.completions.create,
                            model=model,
                            messages=messages,
                            stream=False,
                            timeout=timeout_seconds
                        ),
                        timeout=timeout_seconds
                    )
                except asyncio.TimeoutError:
                    raise Exception(f"API 请求超过 {timeout_seconds} 秒未返回，已自动切换下一个")

                if response is None:
                    raise Exception("API 返回空响应")

                if not hasattr(response, 'choices') or response.choices is None or len(response.choices) == 0:
                    error_msg = "未知错误"
                    if hasattr(response, 'error') and response.error:
                        error_msg = str(response.error)
                    elif hasattr(response, 'model_dump'):
                        error_msg = str(response.model_dump())
                    raise Exception(f"API 返回异常，choices 为空: {error_msg}")

                result = response.choices[0].message.content or ""
                result = result.rstrip("\n")
                ensure_llm_reply_passes_failover_check(result)

                usage = getattr(response, "usage", None)
                total_tokens = (getattr(usage, "total_tokens", 0) if usage else 0) + relay_total_tokens
                prompt_tokens = (getattr(usage, "prompt_tokens", 0) if usage else 0) + relay_prompt_tokens
                completion_tokens = (getattr(usage, "completion_tokens", 0) if usage else 0) + relay_completion_tokens

                self.history.append({
                    "role": "user",
                    "content": filter_sensitive_content(user_content)
                })
                self.history.append({
                    "role": "assistant",
                    "content": result
                })

                self._enforce_message_limit()
                key_manager.mark_success(current_key, model=model, base_url=base_url)
                
                log_api_success(
                    scene=scene,
                    model=display_model,
                    total_tokens=total_tokens,
                    reply=result
                )

                return result, total_tokens, prompt_tokens, completion_tokens

            except Exception as e:
                scene = getattr(self, "session_id", "AI")
                log_api_failure(scene, display_model, current_key, error=str(e))
                error_msg = f"{type(e).__name__}: {e}".lower()
                print(f"[DEBUG] API 调用失败 (key: {current_key[:8]}..., model: {model}): {e}")

                if "429" in error_msg or "rate limit" in error_msg or "rpm limit" in error_msg:
                    key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "503" in error_msg or "busy" in error_msg:
                    key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "500" in error_msg or "502" in error_msg or "504" in error_msg or "timeout" in error_msg or "403" in error_msg:
                    key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "invalid" in error_msg or "unauthorized" in error_msg or "401" in error_msg:
                    if key_manager.is_default_key(current_key, model=model, base_url=base_url):
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    else:
                        key_manager.disable_key(current_key, model=model, base_url=base_url, reason=str(e))
                    last_error = e
                    continue
                elif "model not exist" in error_msg or "not support" in error_msg or "404" in error_msg:
                    if key_manager.is_default_key(current_key, model=model, base_url=base_url):
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    else:
                        key_manager.disable_key(current_key, model=model, base_url=base_url, reason=str(e))
                    last_error = e
                    continue
                elif "quota" in error_msg or "insufficient" in error_msg or "balance" in error_msg or "402" in error_msg:
                    key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "choices" in error_msg:
                    key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "llm 回复命中切换关键词" in str(e).lower():
                    print(f"[LLM Failover] 回复命中关键词，切换下一个 API: model={model}, keyword={str(e)}")
                    key_manager.mark_failure(
                        current_key,
                        model=model,
                        base_url=base_url,
                        reason=str(e),
                        cooldown_seconds=get_api_failure_cooldown_seconds(),
                    )
                    last_error = e
                    continue
                else:
                    key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue

        raise last_error or Exception("所有 API Key 均失败")

    def clear(self):
        """清除上下文（不关闭共享 OpenAI client 池）"""
        self.history.clear()

    def add_message(self, role: str, content: str):
        """添加消息到历史，仅允许 user / assistant"""
        content = filter_sensitive_content(content)
        if role in ["user", "assistant"]:
            self.history.append({"role": role, "content": content})
        self._enforce_message_limit()

    def get_message_count(self):
        return len(self.history) // 2

    def get_stats(self) -> dict:
        return {
            "total_tokens": int(getattr(self, "total_tokens", 0) or 0),
            "total_calls": int(getattr(self, "total_calls", 0) or 0),
        }

    def __del__(self):
        # 类级 client 池不可在实例析构时清空
        return

from bot.memory import ChatMemoryManager


class ContextCompressor:
    """对话上下文动态压缩器"""

    _client_pool: dict = {}  # 类级别共享

    def __init__(self, compression_threshold: int = 40):
        self.compression_threshold = compression_threshold
        self.keep_recent = int(user_cfg.get("compression_keep_recent", 20))
        self.compression_count = {}
        self.last_compression_time = {}
        self.max_sessions = 1000

    def _get_client(self, base_url: str, api_key: str, timeout_seconds: int = None):
        """获取或创建用于压缩摘要的 OpenAI 客户端。不再以 thread_id 为 key，避免重连时无限累积。"""
        cache_key = f"{base_url}_{api_key}"
        if cache_key not in self._client_pool:
            client_timeout = int(timeout_seconds or API_REQUEST_TIMEOUT_SECONDS) + 5
            self._client_pool[cache_key] = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=client_timeout,
                max_retries=1
            )
        return self._client_pool[cache_key]

    def _close_clients(self):
        """压缩器 client 池为类级共享；实例级关闭改为 no-op，避免误伤其他会话。"""
        return

    def _cleanup_old_sessions(self):
        try:
            if len(self.compression_count) > self.max_sessions:
                sorted_sessions = sorted(
                    self.last_compression_time.items(),
                    key=lambda x: x[1]
                )
                sessions_to_remove = sorted_sessions[:len(sorted_sessions) - self.max_sessions]
                for session_id, _ in sessions_to_remove:
                    self.compression_count.pop(session_id, None)
                    self.last_compression_time.pop(session_id, None)
        except Exception as e:
            print(f"清理旧压缩会话失败: {e}")

    async def compress_context(self, context, session_id: str, context_type: str = "group") -> bool:
        """
        压缩普通历史消息。
        注意：
        - 不碰 system_prompt
        - 不再写入 system 角色摘要
        - 压缩后立即保存
        """
        try:
            msg_count = context.get_message_count()
            if msg_count < self.compression_threshold:
                return False

            current_time = time.time()
            last_time = self.last_compression_time.get(session_id, 0)
            if current_time - last_time < 180:
                return False

            history = list(context.history)
            if len(history) < self.keep_recent + 6:
                return False

            if len(history) <= self.keep_recent:
                return False

            to_compress = history[:-self.keep_recent]
            recent_messages = history[-self.keep_recent:]

            # 只压缩 user / assistant
            to_compress = [msg for msg in to_compress if msg.get("role") in ("user", "assistant")]
            if len(to_compress) < 6:
                return False

            summary = await self._generate_summary(to_compress, context_type)
            if not summary:
                summary = self._build_fallback_summary(to_compress, context_type)

            new_history = []
            if summary:
                new_history.append({
                    "role": "assistant",
                    "content": f"[历史摘要，压缩了{len(to_compress)}条消息] {summary}"
                })

            new_history.extend(recent_messages)
            context.history = new_history
            context._enforce_message_limit()

            self.compression_count[session_id] = self.compression_count.get(session_id, 0) + 1
            self.last_compression_time[session_id] = current_time
            self._cleanup_old_sessions()

            if hasattr(context, "_save_memory"):
                context._save_memory()

            return True

        except Exception as e:
            print(f"压缩上下文失败: {e}")
            return False

    def _build_fallback_summary(self, messages: list, context_type: str) -> str:
        """当 AI 压缩摘要失败时，生成一个尽量可读的本地回退摘要。"""
        try:
            cleaned = []
            for msg in messages:
                role = msg.get("role", "user")
                content = str(msg.get("content", "")).strip()
                if not content:
                    continue

                if content.startswith("[历史摘要，压缩了") or content.startswith("[系统自动压缩了"):
                    continue

                content = re.sub(r'\s+', ' ', content)
                if len(content) > 50:
                    content = content[:50] + "..."

                prefix = "用户" if role == "user" else "助手"
                cleaned.append(f"{prefix}：{content}")

            if not cleaned:
                return "历史对话已压缩，早期内容主要为连续交流记录。"

            sample_count = 3 if context_type == "private" else 4
            samples = "；".join(cleaned[:sample_count])
            return f"历史对话已压缩，保留的关键片段包括：{samples}"
        except Exception:
            return "历史对话已压缩，已保留近期上下文。"

    async def _generate_summary(self, messages: list, context_type: str) -> str:
        try:
            message_texts = []
            for msg in messages[-100:]:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                if not content:
                    continue
                if str(content).startswith("[历史摘要，压缩了") or str(content).startswith("[系统自动压缩了"):
                    continue
                if len(content) > 300:
                    content = content[:300] + "..."
                prefix = "用户" if role == "user" else "助手"
                message_texts.append(f"{prefix}: {content}")

            if len(message_texts) < 5:
                return ""

            full_text = "\n".join(message_texts)

            if context_type == "group":
                prompt = f"""请将以下群聊对话记录压缩成一份简洁摘要，保留核心信息和重要上下文：

    对话记录：
    {full_text}

    要求：
    1. 提取核心主题和关键结论
    2. 保留重要约定、决定、未完成事项
    3. 提到关键发言人观点
    4. 控制在100字以内
    5. 不要使用Markdown
    6. 只输出摘要正文"""
            else:
                prompt = f"""请将以下私聊对话记录压缩成一份简洁摘要，保留核心信息和重要上下文：

    对话记录：
    {full_text}

    要求：
    1. 保留主题、约定、承诺、情绪变化
    2. 控制在80字以内
    3. 不要使用Markdown
    4. 只输出摘要正文"""

            system_prompt = "你是一个专业的对话摘要助手，只提炼事实与上下文。"
            api_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]

            max_retries = key_manager.get_attempt_count() or 1
            last_error = None
            tried_keys = set()
            summary = ""

            for attempt in range(max_retries):
                current = key_manager.get_next_for_request(tried_keys=tried_keys, include_cooldown=True)
                if not current:
                    break

                base_url, current_key, model, supports_multimodal, timeout_seconds, display_model = current
                tried_keys.add(key_manager.make_attempt_identity(base_url, current_key, model))

                try:
                    client = self._get_client(base_url, current_key, timeout_seconds)
                    print(f"[DEBUG] 压缩摘要使用 API: model={model}, base_url={base_url}, key={current_key[:8]}...")

                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.chat.completions.create,
                            model=model,
                            messages=api_messages,
                            stream=False,
                            timeout=timeout_seconds
                        ),
                        timeout=timeout_seconds
                    )

                    if response is None:
                        raise Exception("压缩摘要 API 返回空响应")

                    if not hasattr(response, 'choices') or response.choices is None or len(response.choices) == 0:
                        error_msg = "未知错误"
                        if hasattr(response, 'error') and response.error:
                            error_msg = str(response.error)
                        elif hasattr(response, 'model_dump'):
                            error_msg = str(response.model_dump())
                        raise Exception(f"压缩摘要 API 返回异常，choices 为空: {error_msg}")

                    summary = response.choices[0].message.content or ""
                    summary = summary.rstrip("\n")
                    key_manager.mark_success(current_key, model=model, base_url=base_url)
                    break

                except asyncio.TimeoutError:
                    e = Exception(f"压缩摘要 API 请求超过 {timeout_seconds} 秒未返回，已自动切换下一个")
                    error_msg = f"{type(e).__name__}: {e}".lower()
                    print(f"[DEBUG] 压缩摘要 API 调用超时 (key: {current_key[:8]}..., model: {model}): {e}")
                    key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                except Exception as e:
                    error_msg = f"{type(e).__name__}: {e}".lower()
                    print(f"[DEBUG] 压缩摘要 API 调用失败 (key: {current_key[:8]}..., model: {model}): {e}")

                    if "429" in error_msg or "rate limit" in error_msg or "rpm limit" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    elif "503" in error_msg or "busy" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    elif "500" in error_msg or "502" in error_msg or "504" in error_msg or "timeout" in error_msg or "403" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    elif "invalid" in error_msg or "unauthorized" in error_msg or "401" in error_msg:
                        if key_manager.is_default_key(current_key, model=model, base_url=base_url):
                            key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        else:
                            key_manager.disable_key(current_key, model=model, base_url=base_url, reason=str(e))
                    elif "model not exist" in error_msg or "not support" in error_msg or "404" in error_msg:
                        if key_manager.is_default_key(current_key, model=model, base_url=base_url):
                            key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        else:
                            key_manager.disable_key(current_key, model=model, base_url=base_url, reason=str(e))
                    elif "quota" in error_msg or "insufficient" in error_msg or "balance" in error_msg or "402" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    elif "choices" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    else:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())

                    last_error = e
                    continue

            if not summary:
                if last_error:
                    raise last_error
                return ""

            summary = filter_sensitive_content(str(summary)).strip()
            return summary

        except Exception as e:
            print(f"生成压缩摘要失败: {e}")
            traceback.print_exc()
            return ""

    def __del__(self):
        self._close_clients()

    def get_compression_stats(self, session_id: str = None) -> dict:
        if session_id:
            return {
                "compression_count": self.compression_count.get(session_id, 0),
                "last_compression": self.last_compression_time.get(session_id, 0),
                "keep_recent": self.keep_recent,
                "threshold": self.compression_threshold
            }
        else:
            return {
                "total_sessions": len(self.compression_count),
                "total_compressions": sum(self.compression_count.values()),
                "keep_recent": self.keep_recent,
                "threshold": self.compression_threshold,
                "sessions": dict(self.compression_count)
            }



from bot.token_stats import TokenStats, create_token_stats

# 初始化全局Token统计（JSON持久化，重启不丢）
token_stats = create_token_stats()


def add_token_usage(session_id: str, user_id: int = None, group_id: int = None,
                    tokens: int = 0, prompt_tokens: int = 0, completion_tokens: int = 0,
                    model: str = ""):
    """添加真实Token使用记录"""
    token_stats.add_usage(session_id, user_id, group_id, tokens, prompt_tokens, completion_tokens, model)


from bot.trace_store import create_trace_store, classify_error as trace_classify_error

# AI 对话追踪（24 小时滚动窗口，默认关闭，在 WebUI 追踪页开启）
trace_store = create_trace_store()


def is_trace_recording() -> bool:
    """追踪开关：只读内存属性，不碰 config.json，热路径零 IO。"""
    try:
        return bool(trace_store.enabled)
    except Exception:
        return False


def add_trace_record(record: dict) -> str:
    """写入一条 AI 对话追踪记录；任何异常都吞掉，绝不影响回复。"""
    try:
        return trace_store.add_record(record)
    except Exception as e:
        print(f"[Trace] 记录失败（忽略）: {e}")
        return ""


def attach_trace_send(trace_id: str, parts: int, message_ids=None) -> None:
    """回填分段发送结果，失败静默忽略。"""
    try:
        if trace_id:
            trace_store.attach_send(trace_id, parts, message_ids)
    except Exception:
        pass


class EnhancedLimitedDeepSeekContext(LimitedDeepSeekContext):
    """支持动态压缩和持久化的增强版上下文"""

    def __init__(self, system_prompt: str,
                 compressor: ContextCompressor = None,
                 session_id: str = None,
                 context_type: str = "group",
                 chat_id: int = None):
        super().__init__(system_prompt)
        self.compressor = compressor
        self.session_id = session_id
        self.context_type = context_type
        self.chat_id = chat_id
        self.auto_compress_enabled = True
        self.compress_after_messages = int(user_cfg.get("auto_compress_after_messages", 40))
        self.total_tokens = 0
        self.total_calls = 0
        self.last_trace_id = ""
        # 不在 __init__ 里创建 asyncio.Lock，避免绑定到错误的事件循环。
        # Listener.run 重连时可能重建 event loop，因此同时记录锁所属 loop。
        self._lock: asyncio.Lock | None = None
        self._lock_loop = None
        self._lock_init_lock = threading.Lock()

        self._load_memory()

    def set_auto_compress(self, enabled: bool, threshold: int = None):
        self.auto_compress_enabled = bool(enabled)
        if threshold is not None:
            self.compress_after_messages = max(20, min(int(threshold), 80))

    def get_stats(self) -> dict:
        return {
            "total_tokens": int(getattr(self, "total_tokens", 0) or 0),
            "total_calls": int(getattr(self, "total_calls", 0) or 0),
        }

    def _load_memory(self):
        """从文件加载历史记忆，仅加载普通对话历史"""
        try:
            if self.context_type == "private" and self.chat_id:
                history, token_counter = chat_memory.load_private_memory(self.chat_id)
                if history:
                    self.history = [msg for msg in history if msg.get("role") in ("user", "assistant")]
                    self.total_tokens = token_counter
            elif self.context_type == "group" and self.chat_id:
                history, token_counter, group_roles = chat_memory.load_group_memory(self.chat_id)
                if history:
                    self.history = [msg for msg in history if msg.get("role") in ("user", "assistant")]
                    self.total_tokens = token_counter
        except Exception as e:
            print(f"加载记忆失败: {e}")

    def _save_memory(self):
        """保存记忆到文件，仅保存普通对话历史"""
        try:
            clean_history = [msg for msg in self.history if msg.get("role") in ("user", "assistant")]
            if self.context_type == "private" and self.chat_id:
                chat_memory.save_private_memory(self.chat_id, clean_history, self.total_tokens)
            elif self.context_type == "group" and self.chat_id:
                chat_memory.save_group_memory(self.chat_id, clean_history, self.total_tokens, {})
        except Exception as e:
            print(f"保存记忆失败: {e}")

    async def agen_content(self, message) -> tuple[str, int, int, int]:
        # Listener.run 重连可能切换 event loop；为当前 loop 懒建独立锁。
        current_loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not current_loop:
            with self._lock_init_lock:
                if self._lock is None or self._lock_loop is not current_loop:
                    self._lock = asyncio.Lock()
                    self._lock_loop = current_loop
        async with self._lock:
            """
            异步生成内容，自动保存记忆，并在需要时执行压缩
            """
            max_retries = key_manager.get_attempt_count() or 1
            last_error = None
            tried_keys = set()

            # ==== 追踪采集：开关一次性取值，关闭时后续全部退化为布尔判断 ====
            _t_on = is_trace_recording()
            _t_started = time.time()
            _t_attempt_started = _t_started
            _t_attempts = []
            # 预置：_enforce_message_limit / 图片 relay 抛错时 user_content、messages 尚未定义
            _t_user_content = ""
            _t_system_prompt = ""
            _t_msg_count = 0
            _t_images = 0
            _t_history = []

            for attempt in range(max_retries):
                has_image_message = isinstance(message, dict) and bool(message.get("image_urls"))
                direct_image_mode = has_image_message and get_multimodal_image_mode() == "direct"
                if direct_image_mode:
                    current = key_manager.get_next_multimodal_for_request(
                        tried_keys=tried_keys,
                        include_cooldown=True,
                        preferred_model=get_configured_multimodal_model(),
                    )
                    if not current:
                        current = key_manager.get_next_for_request(
                            tried_keys=tried_keys,
                            include_cooldown=True,
                            require_multimodal=False,
                        )
                else:
                    use_multimodal_directly = has_image_message and bool(key_manager.is_default_multimodal())
                    require_multimodal = use_multimodal_directly
                    current = key_manager.get_next_for_request(
                        tried_keys=tried_keys,
                        include_cooldown=True,
                        require_multimodal=require_multimodal,
                    )
                if not current:
                    break

                base_url, current_key, model, supports_multimodal, timeout_seconds, display_model = current
                tried_keys.add(key_manager.make_attempt_identity(base_url, current_key, model))

                try:
                    _t_attempt_started = time.time()
                    self._enforce_message_limit()
                    image_urls = []
                    relay_total_tokens = 0
                    relay_prompt_tokens = 0
                    relay_completion_tokens = 0
                    if isinstance(message, dict):
                        user_content = str(message.get("text", "") or "")
                        raw_image_urls = message.get("image_urls", []) or []
                        if raw_image_urls and not supports_multimodal:
                            if direct_image_mode:
                                user_content = merge_image_relay_into_user_content(user_content, "当前没有可用的多模态模型直接处理图片，图片内容未被读取。")
                            else:
                                image_description, relay_total_tokens, relay_prompt_tokens, relay_completion_tokens = await relay_images_with_multimodal_model(
                                    self,
                                    user_content,
                                    raw_image_urls,
                                )
                                user_content = merge_image_relay_into_user_content(user_content, image_description)
                        else:
                            image_urls = await prepare_image_inputs_for_model(
                                raw_image_urls,
                                supports_multimodal,
                            )
                        messages = self._build_messages()
                        messages.append({
                            "role": "user",
                            "content": build_openai_message_content(
                                build_llm_user_message(user_content),
                                image_urls=image_urls,
                                supports_multimodal=supports_multimodal,
                            )
                        })
                    else:
                        user_content = self._extract_text_from_message(message)
                        messages = self._build_messages(build_llm_user_message(user_content))

                    if _t_on:
                        # messages[0] 才是真正发出去的系统提示词（含 llm_split 追加的后缀）
                        _t_user_content = filter_sensitive_content(user_content)
                        _t_system_prompt = str(messages[0].get("content", "") or "") if messages else ""
                        _t_msg_count = len(messages)
                        _t_images = len(image_urls or [])
                        if not _t_history:
                            # 只在首次尝试采集：重试期间 history 未变，且避免重复计算
                            _t_history = []
                            for m in self.history:
                                text = str(self._clean_content(m.get("content", "")) or "")
                                _t_history.append({
                                    "role": str(m.get("role") or "user"),
                                    "chars": len(text),
                                    "content": text,
                                })

                    client = self._get_client(base_url, current_key, timeout_seconds)

                    scene = getattr(self, "session_id", "AI")
                    log_api_request(
                        scene=scene,
                        model=display_model,
                        base_url=base_url,
                        current_key=current_key,
                        message_count=len(messages),
                        preview=user_content
                    )

                    try:
                        response = await asyncio.wait_for(
                            asyncio.to_thread(
                                client.chat.completions.create,
                                model=model,
                                messages=messages,
                                stream=False,
                                timeout=timeout_seconds
                            ),
                            timeout=timeout_seconds
                        )
                    except asyncio.TimeoutError:
                        raise Exception(f"API 请求超过 {timeout_seconds} 秒未返回，已自动切换下一个")

                    if response is None:
                        raise Exception("API 返回空响应")

                    if not hasattr(response, 'choices') or response.choices is None or len(response.choices) == 0:
                        error_msg = "未知错误"
                        if hasattr(response, 'error') and response.error:
                            error_msg = str(response.error)
                        elif hasattr(response, 'model_dump'):
                            error_msg = str(response.model_dump())
                        raise Exception(f"API 返回异常，choices 为空: {error_msg}")

                    result = response.choices[0].message.content or ""
                    result = result.rstrip("\n")
                    ensure_llm_reply_passes_failover_check(result)

                    usage = getattr(response, "usage", None)
                    total_tokens = (getattr(usage, "total_tokens", 0) if usage else 0) + relay_total_tokens
                    prompt_tokens = (getattr(usage, "prompt_tokens", 0) if usage else 0) + relay_prompt_tokens
                    completion_tokens = (getattr(usage, "completion_tokens", 0) if usage else 0) + relay_completion_tokens
                    # 缓存命中数：OpenAI 兼容接口放在 usage.prompt_tokens_details.cached_tokens，
                    # DeepSeek 等用 prompt_cache_hit_tokens；都没有则为 0（表示该接口不报告命中）。
                    cached_tokens = 0
                    if usage is not None:
                        details = getattr(usage, "prompt_tokens_details", None)
                        if details is not None:
                            cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
                        if not cached_tokens:
                            cached_tokens = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)

                    self.history.append({
                        "role": "user",
                        "content": filter_sensitive_content(user_content)
                    })
                    self.history.append({
                        "role": "assistant",
                        "content": result
                    })

                    self._enforce_message_limit()
                    key_manager.mark_success(current_key, model=model, base_url=base_url)
                
                    log_api_success(
                        scene=scene,
                        model=display_model,
                        total_tokens=total_tokens,
                        reply=result
                    )

                    self.total_tokens += total_tokens
                    self.total_calls += 1

                    if self.context_type == "private" and self.chat_id:
                        add_token_usage(
                            self.session_id,
                            user_id=self.chat_id,
                            tokens=total_tokens,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            model=model
                        )
                    elif self.context_type == "group" and self.chat_id:
                        add_token_usage(
                            self.session_id,
                            group_id=self.chat_id,
                            tokens=total_tokens,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            model=model
                        )
                    else:
                        add_token_usage(
                            self.session_id,
                            tokens=total_tokens,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            model=model
                        )

                    self._save_memory()

                    if (
                            is_feature_enabled("compression_commands", True) and
                            self.auto_compress_enabled and
                            self.compressor and
                            self.session_id and
                            self.get_message_count() >= self.compress_after_messages
                    ):
                        await self.compressor.compress_context(
                            self, self.session_id, self.context_type
                        )

                    if _t_on:
                        try:
                            _t_attempts.append({
                                "index": attempt,
                                "started_at": _t_attempt_started,
                                "duration_ms": int((time.time() - _t_attempt_started) * 1000),
                                "base_url": base_url,
                                "model": model,
                                "display_model": display_model,
                                "key_hint": (str(current_key)[:6] + "...") if current_key else "",
                                "multimodal": bool(supports_multimodal),
                                "message_count": _t_msg_count,
                                "ok": True,
                                "category": "success",
                                "error_type": "",
                                "error": "",
                            })
                            self.last_trace_id = add_trace_record({
                                "ok": True,
                                "time": time.time(),
                                "duration_ms": int((time.time() - _t_started) * 1000),
                                "session_id": self.session_id,
                                "context_type": self.context_type,
                                "chat_id": self.chat_id,
                                "system_prompt": _t_system_prompt,
                                "user_message": _t_user_content,
                                "reply": result,
                                "images": _t_images,
                                "history_count": len(_t_history),
                                "history_overview": _t_history,
                                "tokens": {
                                    "total": total_tokens,
                                    "prompt": prompt_tokens,
                                    "completion": completion_tokens,
                                    "cached": cached_tokens,
                                    "relay_total": relay_total_tokens,
                                    "relay_prompt": relay_prompt_tokens,
                                    "relay_completion": relay_completion_tokens,
                                },
                                "model": model,
                                "display_model": display_model,
                                "attempts": _t_attempts,
                            })
                        except Exception as _te:
                            print(f"[Trace] 成功链路记录失败（忽略）: {_te}")

                    return result, total_tokens, prompt_tokens, completion_tokens

                except Exception as e:
                    scene = getattr(self, "session_id", "AI")
                    log_api_failure(scene, display_model, current_key, error=str(e))
                    error_msg = f"{type(e).__name__}: {e}".lower()
                    print(f"[DEBUG] API 调用失败 (key: {current_key[:8]}..., model: {model}): {e}")

                    if _t_on:
                        try:
                            _t_attempts.append({
                                "index": attempt,
                                "started_at": _t_attempt_started,
                                "duration_ms": int((time.time() - _t_attempt_started) * 1000),
                                "base_url": base_url,
                                "model": model,
                                "display_model": display_model,
                                "key_hint": (str(current_key)[:6] + "...") if current_key else "",
                                "multimodal": bool(supports_multimodal),
                                "message_count": _t_msg_count,
                                "ok": False,
                                "category": trace_classify_error(str(e)),
                                "error_type": type(e).__name__,
                                "error": str(e),
                            })
                        except Exception:
                            pass

                    if "429" in error_msg or "rate limit" in error_msg or "rpm limit" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        last_error = e
                        continue
                    elif "503" in error_msg or "busy" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        last_error = e
                        continue
                    elif "500" in error_msg or "502" in error_msg or "504" in error_msg or "timeout" in error_msg or "403" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        last_error = e
                        continue
                    elif "invalid" in error_msg or "unauthorized" in error_msg or "401" in error_msg :
                        if key_manager.is_default_key(current_key, model=model, base_url=base_url):
                            key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        else:
                            key_manager.disable_key(current_key, model=model, base_url=base_url, reason=str(e))
                        last_error = e
                        continue
                    elif "model not exist" in error_msg or "not support" in error_msg or "404" in error_msg:
                        if key_manager.is_default_key(current_key, model=model, base_url=base_url):
                            key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        else:
                            key_manager.disable_key(current_key, model=model, base_url=base_url, reason=str(e))
                        last_error = e
                        continue
                    elif "quota" in error_msg or "insufficient" in error_msg or "balance" in error_msg or "402" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        last_error = e
                        continue
                    elif "choices" in error_msg:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        last_error = e
                        continue
                    elif "llm 回复命中切换关键词" in str(e).lower():
                        print(f"[LLM Failover] 回复命中关键词，切换下一个 API: model={model}, keyword={str(e)}")
                        key_manager.mark_failure(
                            current_key,
                            model=model,
                            base_url=base_url,
                            reason=str(e),
                            cooldown_seconds=get_api_failure_cooldown_seconds(),
                        )
                        last_error = e
                        continue
                    else:
                        key_manager.mark_failure(current_key, model=model, base_url=base_url, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        last_error = e
                        continue

            if _t_on:
                try:
                    self.last_trace_id = add_trace_record({
                        "ok": False,
                        "time": time.time(),
                        "duration_ms": int((time.time() - _t_started) * 1000),
                        "session_id": self.session_id,
                        "context_type": self.context_type,
                        "chat_id": self.chat_id,
                        "system_prompt": _t_system_prompt,
                        "user_message": _t_user_content,
                        "reply": "",
                        "images": _t_images,
                        "history_count": len(_t_history),
                        "history_overview": _t_history,
                        "tokens": {},
                        "model": "",
                        "display_model": "",
                        "attempts": _t_attempts,
                        "error": str(last_error or "所有 API Key 均失败"),
                    })
                except Exception as _te:
                    print(f"[Trace] 失败链路记录失败（忽略）: {_te}")

            raise last_error or Exception("所有 API Key 均失败")


    # ==================== 增强版ContextManager ====================
class EnhancedContextManager:
    """支持动态压缩和持久化的增强版上下文管理器"""

    # 内存中最多同时持有的会话数，超出时淘汰最久未使用的（LRU）
    MAX_PRIVATE = 200
    MAX_GROUPS = 300

    def __init__(self):
        self.groups: OrderedDict[int, EnhancedLimitedDeepSeekContext] = OrderedDict()
        self.private_chats: OrderedDict[int, EnhancedLimitedDeepSeekContext] = OrderedDict()
        self.compressor = ContextCompressor(compression_threshold=int(user_cfg.get("compression_threshold", 40)))

    def _evict(self, cache: OrderedDict, max_size: int):
        """淘汰最久未使用的条目，释放内存。被淘汰的会话数据已持久化到磁盘，下次访问会重新加载。"""
        while len(cache) > max_size:
            _, ctx = cache.popitem(last=False)
            try:
                ctx._save_memory()
                ctx._close_clients()
            except Exception:
                pass

    def get_context(self, uin: int, gid: int, user_nickname: str = None,
                    role_type: str = "girl_friend") -> EnhancedLimitedDeepSeekContext:
        try:
            user_nickname = filter_sensitive_content(user_nickname) if user_nickname else f"用户{uin}"

            if uin == gid:
                if uin not in self.private_chats:
                    system_prompt = self._get_system_prompt(user_nickname)
                    self.private_chats[uin] = EnhancedLimitedDeepSeekContext(
                        system_prompt,
                        compressor=self.compressor,
                        session_id=f"private_{uin}",
                        context_type="private",
                        chat_id=uin
                    )
                    self._evict(self.private_chats, self.MAX_PRIVATE)
                else:
                    # 移到末尾表示最近使用
                    self.private_chats.move_to_end(uin)

                self.private_chats[uin]._enforce_message_limit()
                return self.private_chats[uin]

            else:
                if gid not in self.groups:
                    system_prompt = self._get_system_prompt("群聊会话")
                    self.groups[gid] = EnhancedLimitedDeepSeekContext(
                        system_prompt,
                        compressor=self.compressor,
                        session_id=f"group_{gid}",
                        context_type="group",
                        chat_id=gid
                    )
                    self._evict(self.groups, self.MAX_GROUPS)
                else:
                    self.groups.move_to_end(gid)

                self.groups[gid]._enforce_message_limit()
                return self.groups[gid]

        except Exception as e:
            traceback.print_exc()

            if uin == gid:
                system_prompt = self._get_system_prompt(user_nickname)
                ctx = EnhancedLimitedDeepSeekContext(system_prompt)
                ctx.compressor = self.compressor
                ctx.session_id = f"private_{uin}_fallback"
                ctx.context_type = "private"
                ctx.chat_id = uin
                return ctx
            else:
                system_prompt = self._get_system_prompt("群聊会话")
                ctx = EnhancedLimitedDeepSeekContext(system_prompt)
                ctx.compressor = self.compressor
                ctx.session_id = f"group_{gid}_fallback"
                ctx.context_type = "group"
                ctx.chat_id = gid
                return ctx

    def _get_system_prompt(self, user_name: str) -> str:
        user_name = filter_sensitive_content(user_name)
        current_bot_name = bot_name
        custom_prompt = str(get_runtime_setting("Others.personality_prompt", user_cfg.get("personality_prompt", "")) or "").strip()
        if not custom_prompt:
            raise ValueError("主对话系统提示词为空：请在 config.json 的 Others.personality_prompt 中配置提示词")
        prompt = custom_prompt.replace("{bot_name}", current_bot_name).replace("{user_name}", user_name)
        return filter_sensitive_content(prompt)

    async def force_compress_current_group(self, group_id: int) -> bool:
        if group_id in self.groups:
            ctx = self.groups[group_id]
            session_id = f"group_{group_id}"
            return await self.compressor.compress_context(ctx, session_id, "group")
        return False

    async def force_compress_current_private(self, user_id: int) -> bool:
        if user_id in self.private_chats:
            ctx = self.private_chats[user_id]
            session_id = f"private_{user_id}"
            return await self.compressor.compress_context(ctx, session_id, "private")
        return False

    def clear_group_context(self, gid: int):
        if gid in self.groups:
            self.groups[gid].clear()
            chat_memory.delete_group_memory(gid)
            del self.groups[gid]

    def clear_private_context(self, uid: int):
        if uid in self.private_chats:
            self.private_chats[uid].clear()
            chat_memory.delete_private_memory(uid)
            del self.private_chats[uid]

    def get_compression_stats(self, session_id: str = None):
        return self.compressor.get_compression_stats(session_id)

    def get_all_sessions_status(self) -> str:
        status = "===== 会话记忆状态 =====\n"
        status += f"📁 记忆存储目录: data/ai_memory\n"
        status += f"⚙️ 压缩设置: 触发{self.compressor.compression_threshold}条, 保留{self.compressor.keep_recent}条\n"
        status += f"🎯 系统提示词: 独立存储，不占用消息数\n\n"

        sessions = chat_memory.get_all_sessions()

        status += "【私聊会话】\n"
        for uid in sessions['private']:
            ctx = self.private_chats.get(uid)
            if ctx:
                msg_count = ctx.get_message_count()
                stats = self.compressor.get_compression_stats(f"private_{uid}")
                token_stats_ctx = ctx.get_stats() if hasattr(ctx, 'get_stats') else {"total_tokens": 0}
                status += f"👤 用户 {uid}: {msg_count}条对话, 压缩{stats.get('compression_count', 0)}次, 消耗{token_stats_ctx['total_tokens']} Token\n"
            else:
                status += f"💾 用户 {uid}: (已存储, 未加载)\n"

        status += "\n【群聊会话】\n"
        for gid in sessions['group']:
            ctx = self.groups.get(gid)
            if ctx:
                msg_count = ctx.get_message_count()
                stats = self.compressor.get_compression_stats(f"group_{gid}")
                token_stats_ctx = ctx.get_stats() if hasattr(ctx, 'get_stats') else {"total_tokens": 0}
                status += f"👥 群 {gid}: {msg_count}条对话, 压缩{stats.get('compression_count', 0)}次, 消耗{token_stats_ctx['total_tokens']} Token\n"
            else:
                status += f"💾 群 {gid}: (已存储, 未加载)\n"

        return status


# ==================== 压缩统计函数定义 ====================
def save_compression_stats(compressor=None):
    try:
        if compressor is None:
            global_vars = globals()
            if 'cmc' in global_vars and hasattr(global_vars['cmc'], 'compressor'):
                compressor = global_vars['cmc'].compressor
            else:
                return False

        stats = compressor.get_compression_stats()

        os.makedirs(os.path.join(str(BASE_DIR), "data", 'compression'), exist_ok=True)
        stats_path = os.path.join(str(BASE_DIR), "data", 'compression', 'compression_stats.json')

        serializable_stats = {
            "total_sessions": stats.get("total_sessions", 0),
            "total_compressions": stats.get("total_compressions", 0),
            "keep_recent": stats.get("keep_recent", 20),
            "threshold": stats.get("threshold", 40),
            "sessions": {},
            "last_compression_times": {},
            "save_time": time.time(),
            "version": "2.1"
        }

        for session_id, count in stats.get("sessions", {}).items():
            serializable_stats["sessions"][str(session_id)] = count

        if hasattr(compressor, 'last_compression_time'):
            for session_id, timestamp in compressor.last_compression_time.items():
                serializable_stats["last_compression_times"][str(session_id)] = timestamp

        atomic_write_json(stats_path, serializable_stats, indent=2)

        return True

    except Exception as e:
        return False


def load_compression_stats(compressor=None):
    try:
        stats_path = os.path.join(str(BASE_DIR), "data", 'compression', 'compression_stats.json')

        if not os.path.exists(stats_path):
            return {} if compressor is None else False

        with open(stats_path, 'r', encoding='utf-8') as f:
            loaded_stats = json.load(f)

        if compressor is not None:
            if hasattr(compressor, 'compression_count'):
                for session_id, count in loaded_stats.get("sessions", {}).items():
                    compressor.compression_count[session_id] = count

            if hasattr(compressor, 'last_compression_time'):
                for session_id, timestamp in loaded_stats.get("last_compression_times", {}).items():
                    compressor.last_compression_time[session_id] = timestamp

            if hasattr(compressor, 'keep_recent'):
                compressor.keep_recent = loaded_stats.get("keep_recent", 20)
            if hasattr(compressor, 'compression_threshold'):
                compressor.compression_threshold = loaded_stats.get("threshold", 40)

            return True
        else:
            return loaded_stats

    except Exception as e:
        return {} if compressor is None else False


def init_compression_stats():
    global cmc
    try:
        if 'cmc' not in globals():
            return False
        if not hasattr(cmc, 'compressor'):
            return False
        return load_compression_stats(cmc.compressor)
    except Exception as e:
        return False


def save_all_ai_memories() -> bool:
    """保存当前内存中已加载的全部私聊/群聊上下文。"""
    manager = globals().get("cmc")
    if manager is None:
        return True
    ok = True
    contexts = list(getattr(manager, "private_chats", {}).values()) + list(getattr(manager, "groups", {}).values())
    for ctx in contexts:
        try:
            ctx._save_memory()
        except Exception as e:
            ok = False
            print(f"保存 AI 会话记忆失败: {e}")
    return ok


# ==================== 信号处理函数 ====================
def signal_handler(signum, frame):
    """处理退出信号"""
    global running
    try:
        stop_webui()
    except Exception:
        pass
    print(f"\n收到信号 {signum}，正在优雅退出...")
    running = False
    
    # 关闭所有AI客户端连接
    try:
        if 'cmc' in globals():
            for ctx in cmc.private_chats.values():
                ctx._close_clients()
            for ctx in cmc.groups.values():
                ctx._close_clients()
    except:
        pass
    
    save_all_ai_memories()
    save_summary_records()
    save_compression_stats(cmc.compressor if 'cmc' in globals() else None)
    print("✅ 所有记忆已保存")
    sys.exit(0)


# 注册信号处理
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ==================== 【修复】/reset 命令处理函数 ====================
async def handle_reset_command(event, actions, is_group=True):
    try:
        if is_group:
            group_id = event.group_id
            user_id = event.user_id
            if group_id in cmc.groups:
                cmc.clear_group_context(group_id)
                await actions.send(group_id=group_id,
                                   message=Manager.Message(
                                       Segments.Text("✅ 已清除本群的对话记忆，让我们重新开始吧~ (｡•ᴗ-)")))
            else:
                await actions.send(group_id=group_id,
                                   message=Manager.Message(Segments.Text("📭 当前群聊没有与我相关的对话记忆")))
            nike = await get_nickname_by_userid(user_id, Manager, actions, group_id)
            add_message(str(group_id), nike, "/reset")
        else:
            user_id = event.user_id
            if user_id in cmc.private_chats:
                cmc.clear_private_context(user_id)
                await actions.send(user_id=user_id,
                                   message=Manager.Message(
                                       Segments.Text("✅ 已清除与你的对话记忆，让我们重新开始吧~ (｡•ᴗ-)")))
            else:
                await actions.send(user_id=user_id,
                                   message=Manager.Message(Segments.Text("📭 当前没有与你相关的对话记忆")))
            nike = await get_nickname_by_userid(user_id, Manager, actions)
        return True
    except Exception as e:
        return False


# ==================== 名言命令处理函数 ====================
async def handle_quote_command(event, actions, is_group=True):
    """处理名言命令 - 引用消息生成名言图片"""
    try:
        if not isinstance(event.message[0], Segments.Reply):
            if is_group:
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Reply(event.message_id),
                                                           Segments.Text(
                                                               "在记录一条名言之前先引用一条消息噢 ☆ヾ(≧▽≦*)o")))
            else:
                await actions.send(user_id=event.user_id,
                                   message=Manager.Message(
                                       Segments.Text("在记录一条名言之前先引用一条消息噢 ☆ヾ(≧▽≦*)o")))
            return True

        msg_id = event.message[0].id
        content = await actions.get_msg(msg_id)
        message_content = content.data["message"]

        imageurl = None
        if isinstance(message_content, list):
            for msg_segment in message_content:
                if hasattr(msg_segment, 'type') and msg_segment.type == 'image':
                    if hasattr(msg_segment, 'file') and msg_segment.file:
                        if str(msg_segment.file).startswith('http'):
                            imageurl = msg_segment.file
                        elif hasattr(msg_segment, 'url') and msg_segment.url:
                            imageurl = msg_segment.url
                    elif hasattr(msg_segment, 'url') and msg_segment.url:
                        imageurl = msg_segment.url
                    break
                elif isinstance(msg_segment, dict) and msg_segment.get('type') == 'image':
                    data = msg_segment.get('data', {})
                    imageurl = data.get('url') or data.get('file')
                    if imageurl and not str(imageurl).startswith('http'):
                        imageurl = data.get('url')
                    break
        elif isinstance(message_content, dict):
            if message_content.get('type') == 'image':
                data = message_content.get('data', {})
                imageurl = data.get('url') or data.get('file')

        quoteimage = await Quote.handle(event.message, actions, imageurl)

        if is_group:
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Reply(event.message_id), quoteimage))
        else:
            await actions.send(user_id=event.user_id,
                               message=Manager.Message(quoteimage))

        try:
            # 修复 #3：Quote 实际写到 temps/quote_<uuid>.png，原硬编码 quote.png 永远删不到。
            # 改为按 mtime 清理过期 quote 图，兼顾本次新生成的文件也会在下次触发时回收。
            cleanup_old_quote_temps()
        except:
            pass

        return True

    except Exception as e:
        traceback.print_exc()

        error_msg = build_user_error_text(e, error_type="program")
        if is_group:
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Reply(event.message_id),
                                                       Segments.Text(error_msg)))
        else:
            await actions.send(user_id=event.user_id,
                               message=Manager.Message(Segments.Text(error_msg)))
        return False


# ==================== 命令匹配工具 ====================
# 命令一律精确匹配，不用 `关键词 in order` 子串匹配。
# 原因：@机器人 / 名字触发 / 概率触发 / 私聊纯文本这些路径下 order 是不带前缀的
# 裸文本，子串匹配会把闲聊误判成命令——例如「我压缩状态不好」会回压缩统计，
# 管理员随口说「清除记忆吧」会真的清空群记忆。
def match_command(order: str, *names: str) -> bool:
    """无参数命令：order 必须与某个名字完全一致。"""
    text = str(order or "").strip()
    return any(text == name for name in names)


def match_command_prefix(order: str, *names: str) -> bool:
    """带参数命令：order 等于名字本身，或以「名字 + 空格」开头。

    空格是必需的，否则 `开` 会命中 `开始吧`。
    """
    text = str(order or "").strip()
    return any(text == name or text.startswith(name + " ") for name in names)


def command_argument(order: str, *names: str) -> str:
    """取命令后面的参数文本，未命中或无参数时返回空串。"""
    text = str(order or "").strip()
    for name in names:
        if text.startswith(name + " "):
            return text[len(name):].strip()
    return ""


# ==================== Token统计命令处理函数 ====================
async def handle_token_command(event, actions, is_group=True, order=""):
    """处理Token统计相关命令"""
    user_id = event.user_id

    if match_command(order, "token统计", "查看token", "token状态"):
        if is_group:
            group_id = event.group_id
            session_id = f"group_{group_id}"

            ctx = cmc.groups.get(group_id)
            session_tokens = 0
            session_calls = 0
            if ctx and hasattr(ctx, 'total_tokens'):
                session_tokens = ctx.total_tokens
                session_calls = ctx.total_calls

            global_stats = token_stats.get_stats()
            group_stats = token_stats.get_stats(group_id=group_id)

            msg = f"📊 Token 消耗统计（过去24小时）\n"
            msg += f"═══════════════\n"
            msg += f"💬 本次对话累计: {session_tokens} Token\n"
            msg += f"   ↳ 调用次数: {session_calls} 次（会话生命周期，非24h）\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"👥 本群24h: {group_stats['group_tokens']} Token\n"
            msg += f"🌐 全局24h: {global_stats['total_tokens']} Token\n"
            msg += f"   ↳ 总调用: {global_stats['total_calls']} 次\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"📈 活跃会话: {global_stats['sessions']} 个\n"
            msg += f"👤 活跃用户: {global_stats['users']} 人\n"
            msg += f"👥 活跃群聊: {global_stats['groups']} 个"

            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Reply(event.message_id),
                                                       Segments.Text(msg)))
        else:
            session_id = f"private_{user_id}"

            ctx = cmc.private_chats.get(user_id)
            session_tokens = 0
            session_calls = 0
            if ctx and hasattr(ctx, 'total_tokens'):
                session_tokens = ctx.total_tokens
                session_calls = ctx.total_calls

            user_stats = token_stats.get_stats(user_id=user_id)
            global_stats = token_stats.get_stats()

            msg = f"📊 Token 消耗统计（过去24小时）\n"
            msg += f"═══════════════\n"
            msg += f"💬 本次对话累计: {session_tokens} Token\n"
            msg += f"   ↳ 调用次数: {session_calls} 次（会话生命周期，非24h）\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"👤 你的24h: {user_stats['user_tokens']} Token\n"
            msg += f"🌐 全局24h: {global_stats['total_tokens']} Token\n"
            msg += f"   ↳ 总调用: {global_stats['total_calls']} 次\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"📈 活跃会话: {global_stats['sessions']} 个\n"
            msg += f"👥 活跃群聊: {global_stats['groups']} 个"

            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(msg)))
        return True

    elif match_command(order, "重置token统计") and is_admin_user(user_id):
        token_stats.reset()
        msg = "✅ 过去24小时 Token 统计已清空"
        if is_group:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(msg)))
        else:
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
        return True

    return False


# ==================== 对话时间线查看器 ====================
def show_conversation_timeline(session_id: str = None):
    print("\n" + "=" * 70)
    print("📋 对话时间线结构")
    print("=" * 70)

    for uid, ctx in cmc.private_chats.items():
        if session_id and f"private_{uid}" != session_id:
            continue
        history = ctx.history
        token_stats_ctx = ctx.get_stats() if hasattr(ctx, 'get_stats') else {"total_tokens": 0, "total_calls": 0}
        print(f"\n👤 私聊会话 [用户{uid}]")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(
            f"📊 总计: {len(history)} 条对话记录 | 💰 消耗: {token_stats_ctx['total_tokens']} Token | 📞 调用: {token_stats_ctx['total_calls']}次")
        print(f"🎯 系统提示词: {ctx.system_prompt[:40]}...（独立存储，不占用消息数）")

        timeline_position = 0
        summary_count = 0

        for i, msg in enumerate(history):
            role = msg.get('role', 'unknown')
            content = msg.get('content', '')

            if content.startswith("[历史摘要，压缩了"):
                summary_count += 1
                match = re.search(r'\[历史摘要，压缩了(\d+)条消息\]', content)
                compressed_count = match.group(1) if match else '?'
                summary_content = content.split(']\n', 1)[-1] if ']\n' in content else content
                print(f"  [{timeline_position:2d}] 📌 历史摘要 #{summary_count} (压缩了{compressed_count}条消息)")
                print(f"      摘要: {summary_content[:100]}...")
            elif role == 'user':
                print(f"  [{timeline_position:2d}] 💬 用户: {content[:40]}...")
            elif role == 'assistant':
                print(f"  [{timeline_position:2d]} 🤖 助手: {content[:40]}...")

            timeline_position += 1

    for gid, ctx in cmc.groups.items():
        if session_id and f"group_{gid}" != session_id:
            continue
        history = ctx.history
        token_stats_ctx = ctx.get_stats() if hasattr(ctx, 'get_stats') else {"total_tokens": 0, "total_calls": 0}
        print(f"\n👥 群聊会话 [群{gid}]")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(
            f"📊 总计: {len(history)} 条对话记录 | 💰 消耗: {token_stats_ctx['total_tokens']} Token | 📞 调用: {token_stats_ctx['total_calls']}次")
        print(f"🎯 系统提示词: {ctx.system_prompt[:40]}...（独立存储，不占用消息数）")

        timeline_position = 0
        summary_count = 0

        for i, msg in enumerate(history):
            role = msg.get('role', 'unknown')
            content = msg.get('content', '')

            if content.startswith("[历史摘要，压缩了"):
                summary_count += 1
                match = re.search(r'\[历史摘要，压缩了(\d+)条消息\]', content)
                compressed_count = match.group(1) if match else '?'
                print(f"  [{timeline_position:2d}] 📌 群聊摘要 #{summary_count} (压缩了{compressed_count}条消息)")
                print(f"      摘要: {content[:100]}...")
            elif role == 'user':
                print(f"  [{timeline_position:2d}] 💬 用户: {content[:40]}...")
            elif role == 'assistant':
                print(f"  [{timeline_position:2d]} 🤖 助手: {content[:40]}...")

            timeline_position += 1


# ==================== 聊天内配置命令 ====================
# 安全子集：只暴露不会改坏连接/Token 的项。键为聊天里输入的中文名。
# 每项 = (配置路径, 类型, 说明)。类型: str/int/float/prob/bool/list
SETTABLE_CONFIG = {
    "中文名": ("Others.bot_name", "str", "机器人中文名"),
    "英文名": ("Others.bot_name_en", "str", "机器人英文名"),
    "前缀": ("Others.reminder", "str", "命令前缀，如 /"),
    "触发词": ("Others.robot_name_triggers", "list", "用逗号分隔，群里提到会触发回复"),
    "群聊概率": ("Others.group_random_reply_probability", "prob", "0~1，群里主动接话概率，0 关闭"),
    "表情冷却": ("Others.emoji_plus_one_cooldown_seconds", "float", "表情+1 防抖秒数"),
    "拍一拍冷却": ("Others.poke_cooldown_seconds", "float", "拍一拍回复防抖秒数"),
    "每日总结次数": ("Others.summary_per_day_limit", "int", "每群每天可总结次数"),
    "单次总结条数": ("Others.summary_max_messages", "int", "单次总结最多读取条数"),
    "压缩阈值": ("Others.compression_threshold", "int", "消息达到多少条允许压缩"),
    "压缩保留": ("Others.compression_keep_recent", "int", "压缩时保留最近多少条"),
    "自动压缩条数": ("Others.auto_compress_after_messages", "int", "累计多少条自动压缩"),
    "弱黑名单概率": ("Others.weak_blacklist_trigger_probability", "prob", "0~1，越小越容易拦截"),
    "AI对话": ("FeatureSwitches.ai_chat", "bool", "总开关，开/关"),
    "私聊": ("FeatureSwitches.private_chat", "bool", "私聊回复，开/关"),
    "群聊": ("FeatureSwitches.group_chat", "bool", "群聊回复，开/关"),
    "敏感词过滤": ("FeatureSwitches.sensitive_filter", "bool", "开/关"),
    "总结功能": ("FeatureSwitches.summary", "bool", "开/关"),
    "表情复读": ("FeatureSwitches.emoji_plus_one", "bool", "开/关"),
    "拍一拍": ("FeatureSwitches.poke_reply", "bool", "开/关"),
    "外部插件": ("FeatureSwitches.plugins_external", "bool", "开/关"),
}


def _coerce_config_value(typ: str, raw: str):
    """把聊天文本转成配置值，返回 (值, 错误信息)。错误时值为 None。"""
    raw = raw.strip()
    if typ == "str":
        return raw, None
    if typ == "list":
        return [s.strip() for s in re.split(r"[，,\s]+", raw) if s.strip()], None
    if typ == "int":
        if not re.fullmatch(r"-?\d+", raw):
            return None, "需要整数"
        return int(raw), None
    if typ in ("float", "prob"):
        try:
            v = float(raw)
        except ValueError:
            return None, "需要数字"
        if typ == "prob":
            if v > 1:  # 兼容 0~100 写法
                v = v / 100.0
            if not (0 <= v <= 1):
                return None, "概率需在 0~1（或 0~100）之间"
        return v, None
    if typ == "bool":
        if raw in ("开", "开启", "on", "true", "1", "是"):
            return True, None
        if raw in ("关", "关闭", "off", "false", "0", "否"):
            return False, None
        return None, "请用 开/关"
    return None, "未知类型"


def _format_config_value(typ: str, value) -> str:
    if typ == "bool":
        return "✅ 开" if value else "❌ 关"
    if typ == "list":
        return "，".join(str(x) for x in (value or []))
    return str(value)


async def handle_config_command(event, actions, is_group=True, order=""):
    """聊天里查看/修改 WebUI 同款配置（安全子集）。仅管理员可改。返回 True 表示已处理。"""
    order = (order or "").strip()
    # 支持快捷别名：/群聊概率 0.05 等效于 /设置 群聊概率 0.05
    for name in SETTABLE_CONFIG:
        if order == name or order.startswith(name + " "):
            order = "设置 " + order
            break
    if not (order == "设置" or order.startswith("设置 ") or order.startswith("设置\n")):
        return False

    user_id = event.user_id
    is_admin = is_admin_user(user_id)

    async def reply(text):
        if is_group:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(text)))
        else:
            await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(text)))

    arg = order[len("设置"):].strip()
    cfg = read_runtime_config()

    # 不带参数：列出全部可设置项与当前值
    if not arg:
        lines = [f"⚙️ 可设置项（{reminder}设置 项目 值）"]
        lines.append("——————————————")
        for name, (path, typ, desc) in SETTABLE_CONFIG.items():
            cur = deep_get_config(cfg, path)
            lines.append(f"{name} = {_format_config_value(typ, cur)}　({desc})")
        lines.append("——————————————")
        lines.append(f"示例：{reminder}设置 群聊概率 0.05")
        if not is_admin:
            lines.append("（仅管理员可修改）")
        await reply("\n".join(lines))
        return True

    if not is_admin:
        await reply("❌ 仅管理员可修改配置")
        return True

    parts = arg.split(None, 1)
    name = parts[0]
    if name not in SETTABLE_CONFIG:
        await reply(f"❌ 未知项目「{name}」\n发送 {reminder}设置 查看全部可设置项")
        return True
    if len(parts) < 2 or not parts[1].strip():
        path, typ, desc = SETTABLE_CONFIG[name]
        cur = deep_get_config(cfg, path)
        await reply(f"{name} 当前 = {_format_config_value(typ, cur)}\n{desc}\n用法：{reminder}设置 {name} 新值")
        return True

    path, typ, desc = SETTABLE_CONFIG[name]
    value, err = _coerce_config_value(typ, parts[1])
    if err:
        await reply(f"❌ {name} 设置失败：{err}")
        return True

    deep_set_config(cfg, path, value)
    if not write_runtime_config(cfg):
        await reply("❌ 写入配置文件失败")
        return True
    try:
        apply_runtime_config()
    except Exception as e:
        await reply(f"⚠️ 已写入配置，但热加载失败：{e}\n重启后生效")
        return True

    await reply(f"✅ 已设置 {name} = {_format_config_value(typ, value)}（已即时生效）")
    return True


def deep_get_config(cfg: dict, path: str):
    cur = cfg
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def deep_set_config(cfg: dict, path: str, value):
    cur = cfg
    parts = path.split(".")
    for key in parts[:-1]:
        if not isinstance(cur.get(key), dict):
            cur[key] = {}
        cur = cur[key]
    cur[parts[-1]] = value


# ==================== 压缩控制命令处理器 ====================
async def handle_compression_commands(event, actions, is_group=True, order=""):
    user_id = event.user_id
    has_permission = False

    if is_admin_user(user_id):
        has_permission = True

    if await handle_token_command(event, actions, is_group, order):
        return True

    if match_command(order, "压缩状态", "压缩统计"):
        if is_group:
            session_id = f"group_{event.group_id}"
            ctx = cmc.groups.get(event.group_id)
            if ctx:
                msg_count = ctx.get_message_count()
                stats = cmc.get_compression_stats(session_id)
                token_stats_ctx = ctx.get_stats() if hasattr(ctx, 'get_stats') else {"total_tokens": 0}

                msg = f"📊 本群对话状态\n"
                msg += f"═════════════════\n"
                msg += f"当前消息数: {msg_count}条\n"
                msg += f"保留最近: {cmc.compressor.keep_recent}条\n"
                msg += f"触发压缩: {ctx.compress_after_messages}条\n"
                msg += f"自动压缩: {'✅ 开启' if ctx.auto_compress_enabled else '❌ 关闭'}\n"
                msg += f"已压缩次数: {stats.get('compression_count', 0)}次\n"
                msg += f"Token消耗: {token_stats_ctx['total_tokens']} Token\n"
                msg += f"记忆存储: ✅ 已保存\n"
                msg += f"系统提示词: ✅ 独立存储（不占用消息数）\n"

                if stats.get('last_compression', 0) > 0:
                    last_time = datetime.datetime.fromtimestamp(stats['last_compression']).strftime('%Y-%m-%d %H:%M:%S')
                    msg += f"上次压缩: {last_time}"
                else:
                    msg += "上次压缩: 从未压缩"
            else:
                msg = "📊 本群尚未产生对话记录"
        else:
            session_id = f"private_{event.user_id}"
            ctx = cmc.private_chats.get(event.user_id)
            if ctx:
                msg_count = ctx.get_message_count()
                stats = cmc.get_compression_stats(session_id)
                token_stats_ctx = ctx.get_stats() if hasattr(ctx, 'get_stats') else {"total_tokens": 0}

                msg = f"📊 当前私聊状态\n"
                msg += f"═════════════════\n"
                msg += f"当前消息数: {msg_count}条\n"
                msg += f"保留最近: {cmc.compressor.keep_recent}条\n"
                msg += f"触发压缩: {ctx.compress_after_messages}条\n"
                msg += f"自动压缩: {'✅ 开启' if ctx.auto_compress_enabled else '❌ 关闭'}\n"
                msg += f"已压缩次数: {stats.get('compression_count', 0)}次\n"
                msg += f"Token消耗: {token_stats_ctx['total_tokens']} Token\n"
                msg += f"记忆存储: ✅ 已保存\n"
                msg += f"系统提示词: ✅ 独立存储（不占用消息数）\n"

                if stats.get('last_compression', 0) > 0:
                    last_time = datetime.datetime.fromtimestamp(stats['last_compression']).strftime('%Y-%m-%d %H:%M:%S')
                    msg += f"上次压缩: {last_time}"
                else:
                    msg += "上次压缩: 从未压缩"
            else:
                msg = "📊 您尚未与机器人产生私聊对话"

        if is_group:
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(msg)))
        else:
            await actions.send(user_id=event.user_id,
                               message=Manager.Message(Segments.Text(msg)))
        return True

    elif match_command(order, "立即压缩", "手动压缩"):
        if not has_permission:
            msg = "❌ 你没有权限执行手动压缩"
            if is_group:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(msg)))
            else:
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
            return True

        if is_group:
            success = await cmc.force_compress_current_group(event.group_id)
            if success:
                msg = "✅ 已手动压缩本群对话，记忆已保存"
            else:
                msg = "❌ 暂时不需要压缩"
        else:
            success = await cmc.force_compress_current_private(event.user_id)
            if success:
                msg = "✅ 已手动压缩当前私聊，记忆已保存"
            else:
                msg = "❌ 暂时不需要压缩"

        if is_group:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(msg)))
        else:
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
        return True

    elif match_command(order, "查看时间线") and has_permission:
        if is_group:
            session_id = f"group_{event.group_id}"
        else:
            session_id = f"private_{event.user_id}"

        import io
        import sys
        old_stdout = sys.stdout
        string_io = io.StringIO()
        sys.stdout = string_io

        show_conversation_timeline(session_id)

        sys.stdout = old_stdout
        output = string_io.getvalue()

        if len(output) > 1500:
            output = output[:1500] + "\n...(消息过长，已截断)"

        if is_group:
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(filter_sensitive_content(output))))
        else:
            await actions.send(user_id=event.user_id,
                               message=Manager.Message(Segments.Text(filter_sensitive_content(output))))
        return True

    elif match_command(order, "查看记忆列表") and has_permission:
        sessions = chat_memory.get_all_sessions()
        msg = "📋 已存储的记忆列表\n"
        msg += "═════════════════\n"
        msg += f"私聊记忆: {len(sessions['private'])}个\n"
        for uid in sessions['private'][:10]:
            msg += f"  👤 用户 {uid}\n"
        if len(sessions['private']) > 10:
            msg += f"  ... 等{len(sessions['private'])}个\n"

        msg += f"\n群聊记忆: {len(sessions['group'])}个\n"
        for gid in sessions['group'][:10]:
            msg += f"  👥 群 {gid}\n"
        if len(sessions['group']) > 10:
            msg += f"  ... 等{len(sessions['group'])}个\n"
        msg += f"\n⚙️ 系统提示词独立存储，不占用消息数"

        if is_group:
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(msg)))
        else:
            await actions.send(user_id=event.user_id,
                               message=Manager.Message(Segments.Text(msg)))
        return True

    elif match_command_prefix(order, "自动压缩"):
        if not has_permission:
            msg = "❌ 你没有权限修改自动压缩设置"
            if is_group:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(msg)))
            else:
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
            return True

        # 参数按空格切分取，不再从整条 order 里捞子串和数字，
        # 避免「自动压缩 开启 40条就压缩」这类输入行为不可预测。
        args = command_argument(order, "自动压缩").split()
        action_word = args[0] if args else ""

        if action_word in ("开启", "启用", "on", "开"):
            enabled = True
            action_msg = "开启"
        elif action_word in ("关闭", "禁用", "off", "关"):
            enabled = False
            action_msg = "关闭"
        else:
            if is_group:
                ctx = cmc.groups.get(event.group_id)
                if ctx:
                    status = "开启" if ctx.auto_compress_enabled else "关闭"
                    msg = f"当前自动压缩: {status}, 触发阈值: {ctx.compress_after_messages}条"
                else:
                    msg = "当前群聊尚未产生对话记录"
            else:
                ctx = cmc.private_chats.get(event.user_id)
                if ctx:
                    status = "开启" if ctx.auto_compress_enabled else "关闭"
                    msg = f"当前自动压缩: {status}, 触发阈值: {ctx.compress_after_messages}条"
                else:
                    msg = "当前私聊尚未产生对话记录"

            if is_group:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(msg)))
            else:
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
            return True

        threshold = 40
        # 阈值取第二个参数，缺省或非法时保持 40
        if len(args) >= 2 and args[1].isdigit():
            threshold = max(20, min(80, int(args[1])))

        if is_group:
            ctx = cmc.groups.get(event.group_id)
            if ctx:
                ctx.set_auto_compress(enabled, threshold)
                msg = f"✅ 已{action_msg}本群自动压缩，触发阈值: {threshold}条"
            else:
                ctx = cmc.get_context(event.user_id, event.group_id, "系统", "girl_friend")
                ctx.set_auto_compress(enabled, threshold)
                msg = f"✅ 已{action_msg}本群自动压缩，触发阈值: {threshold}条"
        else:
            ctx = cmc.private_chats.get(event.user_id)
            if ctx:
                ctx.set_auto_compress(enabled, threshold)
                msg = f"✅ 已{action_msg}私聊自动压缩，触发阈值: {threshold}条"
            else:
                ctx = cmc.get_context(event.user_id, event.user_id, "用户", "girl_friend")
                ctx.set_auto_compress(enabled, threshold)
                msg = f"✅ 已{action_msg}私聊自动压缩，触发阈值: {threshold}条"

        if is_group:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(msg)))
        else:
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
        return True

    elif match_command(order, "清除记忆") and has_permission:
        if is_group:
            cmc.clear_group_context(event.group_id)
            msg = "✅ 已清除本群对话记忆"
        else:
            cmc.clear_private_context(event.user_id)
            msg = "✅ 已清除当前私聊记忆"

        if is_group:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(msg)))
        else:
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
        return True

    elif match_command(order, "全部压缩状态") and is_admin_user(user_id):
        status = cmc.get_all_sessions_status()
        if is_group:
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(status)))
        else:
            await actions.send(user_id=event.user_id,
                               message=Manager.Message(Segments.Text(status)))
        return True

    return False


# ==================== 总结核心功能 ====================
def add_message(group_id: str, user: str, content: str):
    global chat_db
    # 只过滤用户消息内容
    content = filter_sensitive_content(content)
    tokens = estimate_tokens(f"{user}: {content}")
    chat_db[group_id]["history"].append({"user": user, "content": content})
    chat_db[group_id]["token_counter"] += tokens
    return chat_db


def max_summarizable_msgs(group_id: str, max_tokens=800000, db=None) -> int:
    source = db if db is not None else chat_db
    history = source[group_id]["history"]
    total_tokens = 0
    count = 0
    for msg in reversed(history):
        msg_tokens = estimate_tokens(f"{msg['user']}: {msg['content']}")
        if total_tokens + msg_tokens > max_tokens:
            break
        total_tokens += msg_tokens
        count += 1
    return min(count, SUMMARY_MAX_MESSAGES)


def calculate_hot_words(messages, min_count=1, max_words=5, recursion_depth=0):
    if recursion_depth > 20:
        return []

    all_words = []
    stop_words = {
        '的', '了', '是', '在', '我', '有', '和', '就', '不', '人', '都', '一', '一个',
        '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好',
        '自己', '这', '但', '而', '于', '以', '可', '为', '之', '与', '则', '其', '或',
        '即', '因', '及', '由', '时', '等', '所', '并', '且', '着', '呢', '吗', '啊',
        '吧', '呀', '哦', '恩', '嗯', '哈', '嘿', '嘻', '呗', '哒', '啦', '哟', '呼'
    }

    for msg in messages:
        content = filter_sensitive_content(msg['content'])
        words = re.findall(r'(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,4})(?![\u4e00-\u9fff])', content)
        filtered_words = [word for word in words if word not in stop_words]
        all_words.extend(filtered_words)

    word_count = Counter(all_words)

    if len([w for w, c in word_count.items() if c >= min_count]) < 3 and min_count > 1:
        min_count -= 1
        return calculate_hot_words(messages, min_count, max_words, recursion_depth + 1)

    hot_words = [word for word, _ in word_count.most_common(max_words) if _ >= min_count]

    current_min_count = min_count
    while len(hot_words) < 3 and current_min_count > 0:
        current_min_count -= 1
        if current_min_count <= 0:
            hot_words = [word for word, _ in word_count.most_common(max_words) if _ >= 1]
        else:
            hot_words = [word for word, _ in word_count.most_common(max_words) if _ >= current_min_count]

    return hot_words


def generate_chat_summary(group_id):
    global chat_db
    if group_id not in chat_db:
        return f"群：{group_id}\n消息总数：0\n发言人数：0\n热词排行：暂无数据"

    group_data = chat_db[group_id]
    messages = list(group_data['history'])
    message_count = len(messages)

    speakers = set(msg['user'] for msg in messages)
    speaker_count = len(speakers)

    if message_count > 0:
        hot_words = calculate_hot_words(messages, 1, 5)
        if len(hot_words) < 3:
            hot_words = calculate_hot_words(messages, 0, 5)
        hot_words_str = '；'.join(hot_words) if hot_words else "暂无足够热词"
    else:
        hot_words_str = "暂无数据"

    hot_words_str = hot_words_str.replace('图片', '[图片]')
    summary = f"群：{group_id}\n消息总数：{message_count}\n发言人数：{speaker_count}\n热词排行：{hot_words_str}"
    return summary


async def handle_summary_request(group_id: str, match, temp_db=None):
    global chat_db, daily_summary_records
    summary_slot_acquired = False

    try:
        if not match or not hasattr(match, 'group'):
            return "❌ 总结命令格式错误"

        group_val = match.group(1)
        if group_val is None:
            return "❌ 无法解析总结数量"

        n = int(group_val)

        if n <= 0 or n > SUMMARY_MAX_MESSAGES:
            return f"❌ 命令格式错误！请总结 {SUMMARY_MAX_MESSAGES} 条以内的消息 (0<N<={SUMMARY_MAX_MESSAGES})"

        if temp_db is None:
            can_summary, message = try_begin_summary(group_id)
            if not can_summary:
                return message
            summary_slot_acquired = True

        db_to_use = temp_db if temp_db else chat_db

        total_tokens = sum(estimate_tokens(f"{msg['user']}: {msg['content']}")
                           for msg in list(db_to_use[group_id]["history"])[-n:])
        max_tokens = 800000

        if total_tokens > max_tokens:
            max_n = max_summarizable_msgs(group_id, max_tokens, db=db_to_use)
            return f"⚠️ 消息过长（{total_tokens} Tokens > 上限{max_tokens}）\n最多可总结{max_n}条消息"

        if len(list(db_to_use[group_id]["history"])) < 5:
            return "⚠️ 消息过少（少于 5 条消息）"

        messages_list = list(db_to_use[group_id]["history"])[-n:]
        filtered_messages = []
        for msg in messages_list:
            filtered_messages.append(f"{msg['user']}: {filter_sensitive_content(msg['content'])}")

        messages = "\n".join(filtered_messages)

        prompt = f'''请根据以下群聊记录生成摘要：

聊天记录：
{messages}

总结要求：
1. 用紧凑的格式呈现，详细但少于{max_tokens // 10}个汉字
2. 关键点或关键决策点需加粗
3. 标注提出重要意见的成员
4. 如果有，请列出未解决的问题
5. 总结后给出建议或方案
6. 尽量不要使用 Markdown 格式'''

        prompt = filter_sensitive_content(prompt)

        summary_context = LimitedDeepSeekContext(
            "你是一个专业的聊天总结助手，根据聊天记录总结摘要，请不要使用Markdown格式。请用紧凑的格式呈现总结内容。"
        )
        response, _, _, _ = await summary_context.agen_content(prompt)
        response = filter_sensitive_content(response.rstrip("\n"))

        if temp_db is None:
            record_summary(group_id)

        return response
    except Exception as e:
        return build_user_error_text(e, error_type="ai")
    finally:
        if summary_slot_acquired:
            end_summary(group_id)


async def handle_node_messages(data: dict, group_id: str | int = "0"):
    temp_db = defaultdict(lambda: {
        "history": deque(maxlen=1000),
        "token_counter": 0
    })

    def add_to_temp_db(gid: str, user: str, content: str):
        content = filter_sensitive_content(content)
        tokens = estimate_tokens(f"{user}: {content}")
        temp_db[gid]["history"].append({"user": user, "content": content})
        temp_db[gid]["token_counter"] += tokens
        return temp_db

    group_id = str(group_id or "0")
    app_name = "NapCat.Onebot"

    if "NapCat.Onebot" in app_name:
        message_count = 0
        if 'messages' in data:
            for message_item in data['messages']:
                sender = message_item.get('sender', {})
                nickname = sender.get('nickname', str(message_item.get('user_id', '')))
                nickname = filter_sensitive_content(nickname)
                message_list = message_item.get('message', [])

                text_parts = []
                for message_content in message_list:
                    if message_content.get('type') == 'text':
                        text_data = message_content.get('data', {})
                        text = text_data.get('text', '')
                        if text:
                            text_parts.append(filter_sensitive_content(text))

                full_text = ''.join(text_parts)

                if full_text:
                    add_to_temp_db(str(group_id), nickname, full_text)
                    message_count += 1

        elif 'data' in data and 'messages' in data['data']:
            for message_item in data['data']['messages']:
                sender = message_item.get('sender', {})
                nickname = sender.get('nickname', str(message_item.get('user_id', '')))
                nickname = filter_sensitive_content(nickname)
                message_list = message_item.get('message', [])

                text_parts = []
                for message_content in message_list:
                    if message_content.get('type') == 'text':
                        text_data = message_content.get('data', {})
                        text = text_data.get('text', '')
                        if text:
                            text_parts.append(filter_sensitive_content(text))

                full_text = ''.join(text_parts)

                if full_text:
                    add_to_temp_db(str(group_id), nickname, full_text)
                    message_count += 1
    else:
        message_count = 0
        for message_node in data['message']:
            if message_node.get('type') == 'node':
                node_data = message_node.get('data', {})
                nickname = node_data.get('nickname', node_data.get('user_id', ''))
                nickname = filter_sensitive_content(nickname)
                content_list = node_data.get('content', [])

                text_parts = []
                for content_item in content_list:
                    if content_item.get('type') == 'text':
                        text_data = content_item.get('data', {})
                        text = text_data.get('text', '')
                        if text:
                            text_parts.append(filter_sensitive_content(text))

                full_text = ''.join(text_parts)

                if full_text:
                    add_to_temp_db(str(group_id), nickname, full_text)
                    message_count += 1

    return temp_db




async def process_and_send(actions, event, ai_reply: str, is_group: bool, reply_to_first: bool = True,
                           trace_id: str = ""):
    """
    处理AI回复，按分隔符拆分为多条消息并延迟发送
    支持带空格、大小写变体的 <split> 分隔符

    trace_id 为可选参数：传入时把分段数与 message_id 回填到对应追踪记录。
    """
    parts = split_llm_reply_for_send(ai_reply)
    log_console("SEND", f"准备发送 {'群' if is_group else '私聊'} {len(parts)}段 {_short_text(ai_reply, 70)}")

    if not parts:
        return

    # 群聊中如果启用了 <split> 分段首段引用功能，则第一段自动引用触发者消息。
    # 但只有当前事件本身带有 message_id（普通消息事件）时才允许引用，
    # 避免在 NotifyEvent（如拍一拍）等无 message_id 的事件中触发异常。
    split_quote_enabled = is_split_reply_quote_enabled(event.group_id) if is_group else False
    event_message_id = getattr(event, "message_id", None)
    can_reply_message = bool(is_group and event_message_id)
    # 普通回复引用仍由调用方的 reply_to_first 决定。
    # split_reply_quote 只额外控制“多段回复时默认首段引用”，避免误伤单段回复逻辑。
    should_reply_first = can_reply_message and bool(reply_to_first)
    should_quote_split_first = can_reply_message and split_quote_enabled and len(parts) > 1

    sent_message_ids = []

    for idx, text in enumerate(parts):
        should_reply_current = idx == 0 and (should_reply_first or should_quote_split_first)

        if is_group:
            if should_reply_current:
                msg = Manager.Message(Segments.Reply(event_message_id), Segments.Text(text))
            else:
                msg = Manager.Message(Segments.Text(text))

            ret = await actions.send(group_id=event.group_id, message=msg)
        else:
            ret = await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(text)))

        if trace_id:
            try:
                raw = getattr(getattr(ret, "data", None), "raw", None)
                if isinstance(raw, dict) and raw.get("message_id") is not None:
                    sent_message_ids.append(raw.get("message_id"))
            except Exception:
                pass

        if idx < len(parts) - 1:
            delay = random.uniform(1.5, 3.5)
            await asyncio.sleep(delay)

    attach_trace_send(trace_id, len(parts), sent_message_ids)

# ==================== 拍一拍事件处理 ====================
def can_trigger_poke(event) -> bool:
    """检查拍一拍是否处于冷却时间内。"""
    try:
        current_time = time.time()
        if hasattr(event, 'group_id') and event.group_id:
            cooldown_key = f"group:{event.group_id}:user:{getattr(event, 'user_id', '0')}"
        else:
            cooldown_key = f"private:{getattr(event, 'user_id', '0')}"

        last_trigger_time = poke_cooldowns.get(cooldown_key, 0)
        if current_time - last_trigger_time < POKE_COOLDOWN_SECONDS:
            return False

        poke_cooldowns[cooldown_key] = current_time

        if len(poke_cooldowns) > 1000:
            expire_before = current_time - POKE_COOLDOWN_SECONDS
            expired_keys = [key for key, value in poke_cooldowns.items() if value < expire_before]
            for key in expired_keys:
                poke_cooldowns.pop(key, None)

        return True
    except Exception:
        return True


async def handle_private_poke_event(event, actions):
    """处理私聊拍一拍事件"""
    try:
        if not POKE_REPLY_ENABLED:
            return
        user_id = event.user_id
        user_info = await actions.get_stranger_info(user_id)
        user_nickname = filter_sensitive_content(user_info.data.raw.get('nickname', f"用户{user_id}"))

        poke_prompt = f"用户{user_nickname}拍了拍你"
        deepseek_context = cmc.get_context(user_id, user_id, user_nickname)

        response, total_tokens, prompt_tokens, completion_tokens = await deepseek_context.agen_content(poke_prompt)
        response = response.rstrip("\n")

        if not response:
            response = f"被{user_nickname}拍到了~"
        elif len(response) > 200:
            response = f"被{user_nickname}拍到了！"

        await process_and_send(actions, event, filter_sensitive_content(response), is_group=False,
                               trace_id=getattr(deepseek_context, "last_trace_id", ""))


    except Exception as e:
        traceback.print_exc()
        await send_error_detail(actions, event, e, is_group=False, error_type="ai")
        return


async def handle_group_poke_event(event, actions):
    """处理群聊拍一拍事件"""
    try:
        if not POKE_REPLY_ENABLED:
            return
        group_id = event.group_id
        user_id = event.user_id

        try:
            member_info = await actions.get_group_member_info(group_id=group_id, user_id=user_id)
            group_card = member_info.data.raw.get('card', '') or member_info.data.raw.get('nickname', '')
            if group_card:
                display_name = filter_sensitive_content(group_card)
            else:
                user_info = await actions.get_stranger_info(user_id)
                display_name = filter_sensitive_content(user_info.data.raw.get('nickname', f"用户{user_id}"))
        except Exception:
            try:
                user_info = await actions.get_stranger_info(user_id)
                display_name = filter_sensitive_content(user_info.data.raw.get('nickname', f"用户{user_id}"))
            except Exception:
                display_name = f"用户{user_id}"

        poke_prompt = f"用户{display_name}在群聊会话中拍了拍你"
        deepseek_context = cmc.get_context(user_id, group_id, display_name)

        response, total_tokens, prompt_tokens, completion_tokens = await deepseek_context.agen_content(poke_prompt)
        response = response.rstrip("\n")

        if not response:
            response = f"哎呀，被{display_name}拍到了~"
        elif len(response) > 200:
            response = f"被{display_name}拍到了！(◕ᴗ◕✿)"

        await process_and_send(actions, event, filter_sensitive_content(response), is_group=True, reply_to_first=False,
                               trace_id=getattr(deepseek_context, "last_trace_id", ""))

    except Exception as e:
        traceback.print_exc()
        await send_error_detail(actions, event, e, is_group=True, reply=False, error_type="ai")
        return



# ==================== 私聊消息处理 ====================
async def handle_private_message(event: Events.PrivateMessageEvent, actions: Listener.Actions):
    global user_lists, EnableNetwork, generating, Super_User, Manage_User, ROOT_User, emoji_send_count

    user_message = filter_sensitive_content(str(event.message))
    user_id = event.user_id

    # 管理员权限组（方便插件使用）
    ADMINS = ROOT_User[:]
    SUPERS = ADMINS

    try:
        event_user = (await actions.get_stranger_info(user_id)).data.raw
        event_user_nickname = filter_sensitive_content(event_user['nickname'])
    except:
        event_user_nickname = "用户"

    log_receive_private(user_id, event_user_nickname, event.message)

    # ==================== 插件基础上下文（私聊） ====================
    base_plugin_context = build_plugin_base_context(actions, event, ADMINS, SUPERS)

    # ==================== 执行 Any 插件 ====================
    # 注意：总 handler 对私聊已跑过 Any 插件，这里不再重复执行，避免双发。
    plugin_context = base_plugin_context.copy()
    plugin_context.update({
        "event": event,
        "actions": actions,
        "user_id": user_id,
        "group_id": None,
        "user_message": user_message,
        "order": "",
        "is_group": False,
    })

    if user_message == "ping":
        await actions.send(user_id=user_id, message=Manager.Message(Segments.Text("pong! 私聊测试成功！v(◦'ωˉ◦)~♡")))
        return

    if is_feature_enabled("emoji_plus_one", True) and EMOJI_PLUS_ONE_ENABLED and has_emoji(user_message):
        if _is_emoji_plus_one_available(user_id, is_group=False):
            await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(user_message)))
        return

    if user_message == "/reset" or user_message == "重置":
        await handle_reset_command(event, actions, is_group=False)
        return

    should_trigger = False
    order = ""

    if user_message.startswith(reminder):
        order_i = user_message.find(reminder)
        if order_i != -1:
            order = user_message[order_i + len(reminder):].strip()
            if order:
                should_trigger = True

    # 处理压缩相关命令（私聊中也可用）
    if is_feature_enabled("compression_commands", True) and await handle_compression_commands(event, actions, is_group=False, order=order):
        return

    if await handle_config_command(event, actions, is_group=False, order=order):
        return

    # ==================== 插件管理命令（私聊） ====================
    if is_feature_enabled("plugin_admin_commands", False) and user_message.startswith(reminder):
        if f"{reminder}重载插件" == user_message and str(user_id) in ADMINS:
            global plugins, loaded_plugins, disabled_plugins, failed_plugins, plugins_help
            plugins = load_plugins()
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(f"✅ 插件重载完成，当前 {len(loaded_plugins)} 个插件已加载")))
            return
        elif user_message.startswith(f"{reminder}禁用插件 ") and str(user_id) in ADMINS:
            parts = user_message.split("禁用插件")
            if len(parts) > 1:
                plugin_name = parts[-1].strip()
                # 查找插件文件/目录
                found_path = None
                for ext in ["", ".py", ".pyw"]:
                    path = os.path.join(PLUGIN_FOLDER, plugin_name + ext)
                    if os.path.exists(path):
                        found_path = path
                        break
                if not found_path:
                    dir_path = os.path.join(PLUGIN_FOLDER, plugin_name)
                    if os.path.isdir(dir_path):
                        found_path = dir_path
                if found_path:
                    dirname, basename = os.path.split(found_path)
                    new_name = "d_" + basename
                    new_path = os.path.join(dirname, new_name)
                    try:
                        os.rename(found_path, new_path)
                        plugins = load_plugins()
                        await actions.send(user_id=user_id,
                                           message=Manager.Message(Segments.Text(f"✅ 插件 {plugin_name} 已禁用")))
                    except Exception as e:
                        await actions.send(user_id=user_id,
                                           message=Manager.Message(Segments.Text(f"❌ 禁用失败: {e}")))
                else:
                    await actions.send(user_id=user_id,
                                       message=Manager.Message(Segments.Text(f"❌ 找不到插件 {plugin_name}")))
            else:
                await actions.send(user_id=user_id,
                                   message=Manager.Message(Segments.Text("格式错误，请使用：{reminder}禁用插件 插件名")))
            return
        elif user_message.startswith(f"{reminder}启用插件 ") and str(user_id) in ADMINS:
            parts = user_message.split("启用插件")
            if len(parts) > 1:
                plugin_name = parts[-1].strip()
                # 查找被禁用的插件（以 d_ 开头）
                found_path = None
                for ext in ["", ".py", ".pyw"]:
                    path = os.path.join(PLUGIN_FOLDER, "d_" + plugin_name + ext)
                    if os.path.exists(path):
                        found_path = path
                        break
                if not found_path:
                    dir_path = os.path.join(PLUGIN_FOLDER, "d_" + plugin_name)
                    if os.path.isdir(dir_path):
                        found_path = dir_path
                if found_path:
                    dirname, basename = os.path.split(found_path)
                    original_name = basename[2:]  # 去掉 d_ 前缀
                    original_path = os.path.join(dirname, original_name)
                    try:
                        os.rename(found_path, original_path)
                        plugins = load_plugins()
                        await actions.send(user_id=user_id,
                                           message=Manager.Message(Segments.Text(f"✅ 插件 {plugin_name} 已启用")))
                    except Exception as e:
                        await actions.send(user_id=user_id,
                                           message=Manager.Message(Segments.Text(f"❌ 启用失败: {e}")))
                else:
                    await actions.send(user_id=user_id,
                                       message=Manager.Message(Segments.Text(f"❌ 找不到已禁用的插件 {plugin_name}")))
            else:
                await actions.send(user_id=user_id,
                                   message=Manager.Message(Segments.Text("格式错误，请使用：{reminder}启用插件 插件名")))
            return
        elif f"{reminder}插件视角" == user_message:
            status = f"""🔌 插件视角
——————————————
✅ 已加载插件 ({len(loaded_plugins)}):
{chr(10).join(f"{i+1}. {str(plugin).rsplit('_', 1)[0]}" for i, plugin in enumerate(loaded_plugins)) if loaded_plugins else "无"}

❌ 已禁用插件 ({len(disabled_plugins)}):
{chr(10).join(f"{i+1}. {plugin}" for i, plugin in enumerate(disabled_plugins)) if disabled_plugins else "无"}

⚠️ 加载失败 ({len(failed_plugins)}):
{chr(10).join(f"{i+1}. {plugin}" for i, plugin in enumerate(failed_plugins)) if failed_plugins else "无"}"""
            await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(status)))
            return

        elif f"{reminder}model" == user_message and str(user_id) in ADMINS:
            status_list = key_manager.get_status_list()
            lines = ["🤖 当前 API / Model 列表", "——————————————"]
            lines.append(f"🎯 当前使用: {key_manager.get_current_display()}")
            lines.append("")

            if not status_list:
                lines.append("暂无可用配置")
            else:
                for item in status_list:
                    flag_text = " <- 当前" if item["is_current"] else ""
                    last_error = item["last_error"][:80] if item["last_error"] else "无"
                    lines.append(
                        f"{item['id']}. {item['model']}{flag_text}\n"
                        f"   地址: {item['base_url']}\n"
                        f"   Key: {item['key']}\n"
                        f"   状态: {item['status']}\n"
                        f"   失败次数: {item['fail_count']}\n"
                        f"   最近错误: {last_error}"
                    )

            await actions.send(
                user_id=user_id,
                message=Manager.Message(Segments.Text("\n".join(lines)))
            )
            return

        elif user_message.startswith(f"{reminder}model ") and str(user_id) in ADMINS:
            target = user_message[len(f"{reminder}model "):].strip()
            ok = False

            if target.isdigit():
                ok = key_manager.manual_switch_by_index(int(target))
            else:
                ok = key_manager.manual_switch_by_model(target)

            if ok:
                current_info = key_manager.get_current_display()
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text(f"✅ 已切换成功\n当前: {current_info}"))
                )
            else:
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text(f"❌ 切换失败，未找到可用目标：{target}"))
                )
            return

        elif f"{reminder}modellog" == user_message and str(user_id) in ADMINS:
            logs = key_manager.get_switch_logs(20)
            if not logs:
                content = "📜 暂无 API 切换日志"
            else:
                lines = ["📜 最近 API 切换日志", "——————————————"]
                for log in logs:
                    mode = "手动" if log["manual"] else "自动"
                    lines.append(
                        f"[{log['time']}] {mode} {log['from']} -> {log['to']} | {log['reason']}"
                    )
                content = "\n".join(lines)

            await actions.send(
                user_id=user_id,
                message=Manager.Message(Segments.Text(content))
            )
            return

        elif user_message.startswith(f"{reminder}启用model ") and str(user_id) in ADMINS:
            target = user_message[len(f"{reminder}启用model "):].strip()
            if target.isdigit() and key_manager.enable_key(int(target)):
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text(f"✅ 已启用 model #{target}"))
                )
            else:
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text("❌ 启用失败，请检查编号"))
                )
            return

        elif user_message.startswith(f"{reminder}重置model冷却 ") and str(user_id) in ADMINS:
            target = user_message[len(f"{reminder}重置model冷却 "):].strip()
            if target.isdigit() and key_manager.reset_cooldown(int(target)):
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text(f"✅ 已重置 model #{target} 冷却状态"))
                )
            else:
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text("❌ 重置失败，请检查编号"))
                )
            return



    if is_feature_enabled("quote", True) and match_command_prefix(order, "名言"):
        await handle_quote_command(event, actions, is_group=False)
        return

    image_urls = extract_image_urls_from_message(event.message)
    has_images = bool(image_urls)
    if has_images:
        should_trigger = True

    if order in ("帮助", "help") or user_message in (f"{reminder}帮助", f"{reminder}help"):
        content = f'''命令菜单
——————————————
{reminder}名言 —— 把引用消息生成名言图
/reset 或 重置 —— 清除当前对话
{reminder}设置 —— 查看可设置项
{reminder}设置 项目 值 —— 修改配置（仅管理员）
{reminder}压缩状态 —— 查看压缩状态
{reminder}立即压缩 —— 立刻压缩（仅管理员）
{reminder}自动压缩 [开启/关闭] [阈值] —— 设置自动压缩（仅管理员）
{reminder}查看时间线 —— 查看时间线（仅管理员）
{reminder}查看记忆列表 —— 查看记忆列表（仅管理员）
{reminder}清除记忆 —— 清除记忆（仅管理员）
{reminder}token统计 —— 查看过去24小时 Token 统计
{reminder}重置token统计 —— 清空过去24小时 Token 统计（仅管理员）
{reminder}感知 —— 查看运行状态（仅管理员）
{reminder}重载插件 / 禁用插件 / 启用插件 / 插件视角 —— 插件管理（仅管理员）
{reminder}model / modellog —— 模型管理（仅管理员）
{reminder}启用model / 重置model冷却 —— 恢复或清除冷却（仅管理员）
{reminder}重启 —— 重启机器人（仅管理员）'''
        content += build_plugins_help_section()
        await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(content)))
        return

    elif match_command(order, "关于"):
        about = f'''{bot_name} {bot_name_en} - {project_name}
——————————————
Build Information
Version：{version_name}
Rebuilt from HypeR
'''
        await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(about)))
        return


    elif match_command_prefix(order, "大头照"):
        await actions.send(user_id=user_id, message=Manager.Message(
            Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640")))
        return

    elif match_command(order, "注销"):
        if is_admin_user(user_id):
            cmc.clear_private_context(user_id)
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(f"私聊记忆已清除，{bot_name}重新开始~ (/≧▽≦)/")))
        else:
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(f"仅管理员可操作")))
        return

    elif match_command(order, "重启"):
        if is_admin_user(user_id):
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text("正在保存所有记忆并重启... 🧠💾")))
            try:
                save_restart_state("private", user_id)

            except:
                pass
            try:
                stop_webui()
            except Exception:
                pass
            Listener.restart()
        else:
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text("仅管理员可操作")))
        return

    elif match_command(order, "感知"):
        if is_admin_user(user_id):
            system_info = get_system_info()
            sessions = chat_memory.get_all_sessions()
            feel = f'''{bot_name} {bot_name_en} - 私聊模式
            
    
——————————————
System Now
运行时间: {seconds_to_hms(round(time.time() - second_start, 2))}
系统版本: {system_info["version_info"]}
CPU使用: {str(system_info["cpu_usage"]) + "%"}
内存使用: {str(system_info["memory_usage_percentage"]) + "%"}
——————————————
记忆存储
私聊记忆: {len(sessions['private'])}个
群聊记忆: {len(sessions['group'])}个
压缩次数: {sum(cmc.compressor.compression_count.values())}次
Token总计: {token_stats.total_tokens} Token（过去24小时）
系统提示词: ✅ 独立存储'''
            for i, usage in enumerate(system_info["gpu_usage"]):
                feel = feel + f"\nGPU {i} 使用: {usage * 100:.2f}%"
            await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(feel)))
        else:
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(f"仅管理员可操作")))
        return


    elif should_trigger and order:
        # 先执行普通插件（非 Any）
        plugin_context = base_plugin_context.copy()
        plugin_context.update({
            "event": event,
            "actions": actions,
            "user_id": user_id,
            # 私聊没有群号，但显式给 None：插件开发指南里的示例把 group_id
            # 写成必需参数，不给这个键会让照抄的插件在私聊直接报缺参
            "group_id": None,
            "user_message": user_message,
            "order": order,
            "is_group": False,
        })
        if is_feature_enabled("plugins_external", True) and await execute_plugins(False, **plugin_context):
            return

        if not (is_feature_enabled("ai_chat", True) and is_feature_enabled("private_chat", True)):
            return

        try:
            final_message = build_private_ai_text_message(event_user_nickname, order)
            deepseek_context = cmc.get_context(user_id, user_id, event_user_nickname)

            current_count = deepseek_context.get_message_count()

            deepseek_context._enforce_message_limit()

            result, total_tokens, prompt_tokens, completion_tokens = await deepseek_context.agen_content({"text": final_message, "image_urls": image_urls})
            result = result.rstrip("\n")

            new_count = deepseek_context.get_message_count()

            await process_and_send(actions, event, result, is_group=False,
                                   trace_id=getattr(deepseek_context, "last_trace_id", ""))


        except Exception as e:
            traceback.print_exc()
            await send_error_detail(actions, event, e, is_group=False, error_type="ai")


    elif not user_message.startswith(reminder) and len(user_message.strip()) > 0:
        # 私聊中直接对话，先执行普通插件
        plugin_context = base_plugin_context.copy()
        plugin_context.update({
            "event": event,
            "actions": actions,
            "user_id": user_id,
            # 私聊没有群号，但显式给 None：插件开发指南里的示例把 group_id
            # 写成必需参数，不给这个键会让照抄的插件在私聊直接报缺参
            "group_id": None,
            "user_message": user_message,
            "order": user_message.strip(),
            "is_group": False,
        })
        if is_feature_enabled("plugins_external", True) and await execute_plugins(False, **plugin_context):
            return

        if not (is_feature_enabled("ai_chat", True) and is_feature_enabled("private_chat", True)):
            return

        try:
            final_message = build_private_ai_text_message(event_user_nickname, user_message.strip())
            deepseek_context = cmc.get_context(user_id, user_id, event_user_nickname)

            current_count = deepseek_context.get_message_count()

            deepseek_context._enforce_message_limit()

            result, total_tokens, prompt_tokens, completion_tokens = await deepseek_context.agen_content({"text": final_message, "image_urls": image_urls})
            result = result.rstrip("\n")

            new_count = deepseek_context.get_message_count()

            await process_and_send(actions, event, filter_sensitive_content(result), is_group=False,
                                   trace_id=getattr(deepseek_context, "last_trace_id", ""))


        except Exception as e:
            traceback.print_exc()
            await send_error_detail(actions, event, e, is_group=False, error_type="ai")


# ==================== 插件加载器 ====================
class LegacyPluginAIAdapter:
    """兼容 Jianer_Next_QQ_Bot 插件里对 AIbot.generate_response 的调用。"""

    @staticmethod
    async def generate_response(enable_network, context_manager, prompt_text, runtime_user_lists, event):
        try:
            if not is_feature_enabled("ai_chat", True):
                return None, None, False
            if hasattr(event, "group_id") and getattr(event, "group_id", None):
                if not is_feature_enabled("group_chat", True):
                    return None, None, False
            else:
                if not is_feature_enabled("private_chat", True):
                    return None, None, False

            event_user_nickname = "用户"
            try:
                sender = getattr(event, "sender", None)
                if isinstance(sender, dict):
                    event_user_nickname = filter_sensitive_content(
                        sender.get("card") or sender.get("nickname") or event_user_nickname
                    )
            except Exception:
                pass

            if hasattr(event, "group_id") and getattr(event, "group_id", None):
                deepseek_context = context_manager.get_context(event.user_id, event.group_id, event_user_nickname)
            else:
                deepseek_context = context_manager.get_context(event.user_id, event.user_id, event_user_nickname)

            final_message = f"【{event_user_nickname}】说：{filter_sensitive_content(str(getattr(event, 'message', '')))}"
            result, total_tokens, prompt_tokens, completion_tokens = await deepseek_context.agen_content(build_llm_user_message(final_message))
            result = (result or "").rstrip("\n")
            if not result:
                return None, None, False

            await process_and_send(
                LegacyPluginAIAdapter._actions,
                event,
                filter_sensitive_content(result),
                is_group=bool(hasattr(event, "group_id") and getattr(event, "group_id", None)),
                trace_id=getattr(deepseek_context, "last_trace_id", ""),
            )
            return None, None, True
        except Exception:
            traceback.print_exc()
            return None, None, False


def build_plugin_base_context(actions, event, ADMINS, SUPERS) -> dict:
    """为旧插件生态补充兼容上下文参数。"""
    return {
        "Manager": Manager,
        "Segments": Segments,
        "ROOT_User": ROOT_User,
        "Super_User": Super_User,
        "Manage_User": Manage_User,
        "bot_name": bot_name,
        "bot_name_en": bot_name_en,
        "reminder": reminder,
        "ONE_SLOGAN": ONE_SLOGAN,
        "ADMINS": ADMINS,
        "SUPERS": SUPERS,
        "os": os,
        "gen_message": globals().get("gen_message"),
        "AIbot": LegacyPluginAIAdapter,
        "EnableNetwork": EnableNetwork,
        "cmc": cmc,
        "sys_prompt": sys_prompt,
        "user_lists": user_lists,
        # 以下为插件化娱乐功能后新增的注入项
        "filter_sensitive_content": filter_sensitive_content,
        "is_feature_enabled": is_feature_enabled,
        "version_name": version_name,
        # 只给拼好的 ws 地址，不注入 config 对象：config.others 含 API key
        "ws_url": f"ws://{config.connection.host}:{config.connection.port}",
    }


def load_plugins():
    global loaded_plugins, disabled_plugins, failed_plugins, plugins, plugins_help, reminder, bot_name
    # 重载前清理上一轮注册的模块，否则每次 /重载插件 或保存配置都会在
    # sys.modules 里留下一批永不回收的模块对象（连带它们持有的全局状态）。
    for stale_name in loaded_plugins:
        sys.modules.pop(stale_name, None)
    plugins = []
    plugins_help = ""
    loaded_plugins.clear()
    disabled_plugins.clear()
    failed_plugins.clear()

    for filename in os.listdir(PLUGIN_FOLDER):
        if filename == "__pycache__":
            continue

        if filename.startswith("d_"):
            disabled_plugins.append(filename[2:] if filename.endswith(".py") else filename)
            continue

        plugin_path = os.path.join(PLUGIN_FOLDER, filename)
        if os.path.isdir(plugin_path):
            setup_file = os.path.join(plugin_path, "setup.py")
            if not os.path.exists(setup_file):
                setup_file = os.path.join(plugin_path, "main.py")
            if os.path.exists(setup_file):
                try:
                    unique_name = f"{filename}_{uuid.uuid4().hex}"
                    spec = importlib.util.spec_from_file_location(unique_name, setup_file)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[unique_name] = module
                    # exec_module 期间插件还拿不到 plugin_context，
                    # 所以版本号与插件名必须在执行前注入，供插件顶层自检使用。
                    module.XCBOT_VERSION = version_name
                    module.PLUGIN_NAME = filename
                    spec.loader.exec_module(module)

                    if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                        if isinstance(module.TRIGGHT_KEYWORD, str):
                            plugins.append(module)
                            loaded_plugins.append(unique_name)
                            if hasattr(module, 'HELP_MESSAGE') and isinstance(module.HELP_MESSAGE, str):
                                for line in module.HELP_MESSAGE.splitlines():
                                    if line.strip():
                                        plugins_help += f"\n       {line.strip()}"
                            print(f"✅ 已加载插件目录: {filename}")
                        else:
                            failed_plugins.append(f"{filename} (TRIGGHT_KEYWORD 必须是字符串)")
                    else:
                        failed_plugins.append(f"{filename} (缺少 TRIGGHT_KEYWORD 或 on_message)")
                except Exception as e:
                    failed_plugins.append(f"{filename} (加载失败: {e})")
                    print(f"❌ 加载插件目录 {filename} 失败: {e}")
            else:
                print(f"⚠️ 目录 {filename} 缺少 setup.py，跳过")
        elif filename.endswith(".py") or filename.endswith(".pyw"):
            module_name = filename[:-3] if filename.endswith(".py") else filename[:-4]
            unique_name = f"{module_name}_{uuid.uuid4().hex}"
            try:
                spec = importlib.util.spec_from_file_location(unique_name, os.path.join(PLUGIN_FOLDER, filename))
                module = importlib.util.module_from_spec(spec)
                sys.modules[unique_name] = module
                module.XCBOT_VERSION = version_name
                module.PLUGIN_NAME = module_name
                spec.loader.exec_module(module)

                if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                    if isinstance(module.TRIGGHT_KEYWORD, str):
                        plugins.append(module)
                        loaded_plugins.append(unique_name)
                        if hasattr(module, 'HELP_MESSAGE') and isinstance(module.HELP_MESSAGE, str):
                            for line in module.HELP_MESSAGE.splitlines():
                                if line.strip():
                                    plugins_help += f"\n       {line.strip()}"
                        print(f"✅ 已加载插件: {module_name}")
                    else:
                        failed_plugins.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                else:
                    failed_plugins.append(f"{module_name} (缺少 TRIGGHT_KEYWORD 或 on_message)")
            except Exception as e:
                failed_plugins.append(f"{module_name} (加载失败: {e})")
                print(f"❌ 加载插件 {module_name} 失败: {e}")
        else:
            print(f"跳过非插件文件: {filename}")

    print(f"✅ 成功加载 {len(loaded_plugins)} 个插件，失败 {len(failed_plugins)} 个")
    return plugins


# ==================== 插件执行器 ====================
def build_plugins_help_section() -> str:
    """把插件的 HELP_MESSAGE 汇总成一段追加到 /帮助 菜单末尾。

    plugins_help 每行已带换行和缩进；为空时返回空串，避免菜单尾部多出空行。
    """
    if not plugins_help.strip():
        return ""
    return "\n——————————————\n🔌 插件命令" + plugins_help


def _make_plugin_logger(plugin_name: str):
    """给插件返回一个带 [插件名] 前缀的日志函数。"""
    def _log(content, tag: str = "PLUGIN"):
        print(f"[{tag}][{plugin_name}] {_short_text(content, 180)}")
    return _log


async def execute_plugins(isAny: bool, **main_context) -> bool:
    """执行插件，若任一插件返回 True 则中断后续处理"""
    user_message = main_context.get("order", "") if "order" in main_context else ""
    is_group = main_context.get("is_group", False)
    # group_chat / private_chat 的语义是"是否触发 AI 对话"，
    # 只应约束响应所有消息的 Any 插件；命令类插件（如 /天气）不受其影响。
    if isAny:
        if is_group:
            if not is_feature_enabled("group_chat", True):
                return False
        else:
            if not is_feature_enabled("private_chat", True):
                return False

    try:
        LegacyPluginAIAdapter._actions = main_context.get("actions")
    except Exception:
        pass

    for plugin_module in plugins:
        trigger = False
        if isAny and plugin_module.TRIGGHT_KEYWORD == "Any":
            trigger = True
        elif not isAny and f"{reminder}{plugin_module.TRIGGHT_KEYWORD}" in f"{reminder}{user_message}":
            trigger = True

        if trigger:
            try:
                # 日志前缀要带插件名，而 build_plugin_base_context 每轮只调一次、
                # 不知道当前是哪个插件，所以 log 在这里按插件动态绑定。
                plugin_name = getattr(plugin_module, "PLUGIN_NAME", None) or plugin_module.__name__
                plugin_context = dict(main_context)
                plugin_context["log"] = _make_plugin_logger(plugin_name)

                sig = inspect.signature(plugin_module.on_message)
                kwargs = {}
                for param_name, param in sig.parameters.items():
                    if param.kind in (
                        inspect.Parameter.VAR_POSITIONAL,
                        inspect.Parameter.VAR_KEYWORD,
                    ):
                        continue

                    if param_name in plugin_context:
                        kwargs[param_name] = plugin_context[param_name]
                    elif param.default is not inspect.Parameter.empty:
                        pass
                    else:
                        raise ValueError(f"插件 {plugin_name} 缺少参数 {param_name}")

                response = await plugin_module.on_message(**kwargs)
                if response is True:
                    return True
            except Exception as e:
                print(f"❌ 插件 {getattr(plugin_module, 'PLUGIN_NAME', plugin_module.__name__)} 执行出错: {e}")
                traceback.print_exc()
                # 插件异常不应吞掉后续 AI/命令处理
                continue
    return False


# ==================== 主事件处理器 ====================
@Listener.reg
@Logic.ErrorHandler().handle_async
async def handler(event: Events.Event, actions: Listener.Actions) -> None:
    global settings_loaded, bot_name, bot_name_en, reminder
    global chat_db, user_lists, second_start, EnableNetwork, generating
    global Super_User, Manage_User, ROOT_User, sys_prompt, emoji_send_count
    global _current_qq_actions, _qq_actions_lock, _current_bot_self_id

    actions = LoggedActions(actions)

    # 持续刷新全局引用，确保重连后仍可发送
    with _qq_actions_lock:
        _current_qq_actions = actions
        self_id = getattr(event, "self_id", None)
        if self_id:
            _current_bot_self_id = self_id

    if hasattr(event, 'user_id') and event.user_id == event.self_id:
        return

    all_blacklist = get_all_blacklist()

    if not settings_loaded:
        Read_Settings()
        settings_loaded = True

    # 管理员权限组（方便插件使用）
    ADMINS = ROOT_User[:]
    SUPERS = ADMINS

    # ==================== 插件基础上下文（群聊） ====================
    base_plugin_context = build_plugin_base_context(actions, event, ADMINS, SUPERS)

    # 构建动态上下文供 Any 插件使用
    plugin_context = base_plugin_context.copy()
    plugin_context.update({
        "event": event,
        "actions": actions,
        "user_id": getattr(event, 'user_id', None),
        "group_id": getattr(event, 'group_id', None),
        "user_message": str(getattr(event, 'message', '')),
        "order": "",
        "is_group": isinstance(event, Events.GroupMessageEvent),
    })
    # 执行 Any 插件
    if is_feature_enabled("plugins_external", True) and await execute_plugins(True, **plugin_context):
        return

    if isinstance(event, Events.GroupMemberIncreaseEvent):
        if not is_feature_enabled("group_join_welcome", True):
            return

        user = event.user_id
        group_id = event.group_id

        if is_user_blacklisted(str(group_id), all_blacklist):
            return

        try:
            user_info = await actions.get_stranger_info(user)
            user_nickname = filter_sensitive_content(user_info.data.raw.get('nickname', f"用户{user}"))

            welcome = render_group_join_welcome_text(user, group_id, user_nickname)

            try:
                await actions.send(
                    group_id=group_id,
                    message=Manager.Message(
                        Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"),
                        Segments.Text("欢迎"),
                        Segments.At(user),
                        Segments.Text(filter_sensitive_content(welcome))
                    )
                )
            except Exception:
                await actions.send(
                    group_id=group_id,
                    message=Manager.Message(
                        Segments.At(user),
                        Segments.Text(f" {filter_sensitive_content(welcome)}")
                    )
                )
        except Exception as e:
            pass
        return

    if isinstance(event, Events.NotifyEvent):
        if hasattr(event, 'notice_type') and event.notice_type == 'notify':
            if hasattr(event, 'sub_type') and event.sub_type == 'poke':
                if event.target_id == event.self_id:
                    if not is_feature_enabled("poke_reply", True):
                        return
                    if hasattr(event, 'group_id') and event.group_id:
                        log_console("RECV", f"群 {event.group_id} {getattr(event, 'user_id', '')} 拍一拍")
                    else:
                        log_console("RECV", f"私聊 {getattr(event, 'user_id', '')} 拍一拍")
                    all_blacklist = get_all_blacklist()
                    if is_user_blacklisted(event.user_id, all_blacklist):
                        return
                    if not can_trigger_poke(event):
                        return

                    if hasattr(event, 'group_id') and event.group_id:
                        await handle_group_poke_event(event, actions)
                    else:
                        await handle_private_poke_event(event, actions)
                    return

    if isinstance(event, Events.PrivateMessageEvent):
        if is_user_blacklisted(event.user_id, all_blacklist):
            return
        await handle_private_message(event, actions)
        return

    if isinstance(event, Events.HyperListenerStartNotify):
        HOT_SWITCH_IN_PROGRESS.clear()
        set_connection_status("connected", "已连接", "OneBot / Hyper 已建立连接")
        restart_state = load_restart_state()
        if restart_state:
            clear_restart_state()
            target_type = restart_state.get("type")
            target_id = restart_state.get("id")

            text = f'''{bot_name} {bot_name_en} - {project_name}
    ——————————————
    Welcome! {bot_name} was restarted successfully. Now you can send {reminder}帮助 to know more.'''

            try:
                if target_type == "private":
                    await actions.send(
                        user_id=int(target_id),
                        message=Manager.Message(Segments.Text(text))
                    )
                elif target_type == "group":
                    await actions.send(
                        group_id=int(target_id),
                        message=Manager.Message(Segments.Text(text))
                    )
            except Exception as e:
                print(f"发送重启恢复通知失败: {e}")
            return

    if isinstance(event, Events.GroupAddInviteEvent):
        keywords: list = user_cfg.get("auto_approval", [])
        cleaned_text = event.comment.strip().lower()

        for keyword6 in keywords:
            processed_keyword = keyword6.strip().lower()
            all_chars_present = True
            for char in processed_keyword:
                if char not in cleaned_text:
                    all_chars_present = False
                    break
            if all_chars_present:
                await actions.set_group_add_request(flag=event.flag, sub_type=event.sub_type, approve=True, reason="")
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Text(
                                       f"用户 {event.user_id} 的答案正确,已自动批准,题目数据为 {filter_sensitive_content(event.comment)}")))
                return

    if isinstance(event, Events.GroupMessageEvent):
        if is_user_blacklisted(event.user_id, all_blacklist) or is_user_blacklisted(event.group_id, all_blacklist):
            return

        user_message = filter_sensitive_content(str(event.message))
        order = ""

        if should_block_by_weak_blacklist(event, user_id=event.user_id, user_message=user_message, is_group=True):
            return

        try:
            event_user_nickname = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id, event)
        except:
            event_user_nickname = f"用户{event.user_id}"

        log_receive_group(event.group_id, event.user_id, event_user_nickname, event.message)

        at_command_text = ""
        has_command_at_bot = False
        for segment in event.message:
            if isinstance(segment, Segments.At) and int(segment.qq) == int(event.self_id):
                has_command_at_bot = True
            elif has_command_at_bot and isinstance(segment, Segments.Text):
                at_command_text += segment.text + " "
        if has_command_at_bot and at_command_text.strip():
            user_message = reminder + at_command_text.strip().lstrip(reminder).strip()


        if user_message == "/reset" or user_message == "重置":
            await handle_reset_command(event, actions, is_group=True)
            return

        if is_feature_enabled("quote", True) and user_message.startswith(f"{reminder}名言"):
            await handle_quote_command(event, actions, is_group=True)
            nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
            chat_db = add_message(event.group_id, nike, user_message)
            return

        # 命令必须以「总结」开头，避免 /看看20群的总结 之类被误判；
        # 数字只从「总结」之后取，同时兼容 /总结以上10条消息 和 /总结 10
        if is_feature_enabled("summary", True) and user_message.startswith(f"{reminder}总结"):
            nums = re.findall(r'\d+', user_message[len(reminder) + len("总结"):])
            if not nums:
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Reply(event.message_id),
                                                           Segments.Text(
                                                               f"❌ 请指定要总结的消息数量，例如：{reminder}总结以上10条消息 (1-{SUMMARY_MAX_MESSAGES}条)")))
                nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
                chat_db = add_message(event.group_id, nike, user_message)
                return

            n = int(nums[0])

            if n <= 0 or n > SUMMARY_MAX_MESSAGES:
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Reply(event.message_id),
                                                           Segments.Text(
                                                               f"❌ 命令格式错误！请总结 {SUMMARY_MAX_MESSAGES} 条以内的消息 (1-{SUMMARY_MAX_MESSAGES}条)")))
                nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
                chat_db = add_message(event.group_id, nike, user_message)
                return

            can_summary, limit_message = can_summary_today(event.group_id)
            if not can_summary:
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Reply(event.message_id),
                                                           Segments.Text(limit_message)))
                nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
                chat_db = add_message(event.group_id, nike, user_message)
                return

            selfID = await actions.send(group_id=event.group_id,
                                        message=Manager.Message(
                                            Segments.Text(f"请等待，{bot_name} 正在总结 {n} 条消息......φ(゜▽゜*)♪")))

            class MockMatch:
                def __init__(self, n):
                    self.n = n

                def group(self, index):
                    return str(self.n) if index == 1 else None

            mock_match = MockMatch(n)

            try:
                if isinstance(event.message[0], Segments.Reply):
                    content = await actions.get_msg(event.message[0].id)
                    msg = gen_message({"message": content.data["message"]})
                    message = None

                    for i in msg:
                        if isinstance(i, Segments.Forward):
                            data = Manager.Ret.fetch(await actions.custom.get_forward_msg(id=i.id)).data.raw
                            node_messages = await handle_node_messages(data, event.group_id)
                            message = await handle_summary_request(event.group_id, mock_match, node_messages)
                            break

                    if not message:
                        message = "❌ 未找到转发的消息！\n请确保引用消息的是一条聊天记录，并确保聊天记录中包含需要总结的消息"
                else:
                    message = await handle_summary_request(event.group_id, mock_match)

                if len(message) < 400:
                    await actions.send(group_id=event.group_id,
                                       message=Manager.Message(Segments.Reply(event.message_id),
                                                               Segments.Text(filter_sensitive_content(message))))
                else:
                    await actions.send_group_forward_msg(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.CustomNode(
                            str(event.self_id),
                            bot_name,
                            Manager.Message(Segments.Text(filter_sensitive_content(message)))
                        ))
                    )

                try:
                    await actions.del_message(selfID.data.message_id)
                except:
                    pass

            except Exception as e:
                error_msg = build_user_error_text(e, error_type="ai")
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Reply(event.message_id),
                                                           Segments.Text(error_msg)))

            nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
            chat_db = add_message(event.group_id, nike, user_message)
            return

        # 同样要求命令开头，避免消息中间提到「数据看板」被误触发
        if is_feature_enabled("summary", True) and (
                user_message.startswith(f"{reminder}聊天数据看板")
                or user_message.startswith(f"{reminder}数据看板")):
            if '@all' in user_message or '@全体' in user_message:
                if not is_admin_user(event.user_id):
                    await actions.send(group_id=event.group_id,
                                       message=Manager.Message(Segments.Text(f"仅管理员可操作")))
                    nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
                    chat_db = add_message(event.group_id, nike, user_message)
                    return

                chat_summary = "===== 全群聊天数据看板 =====\n"
                for group_id in chat_db:
                    group_summary = generate_chat_summary(group_id)
                    chat_summary += f"\n{group_summary}\n{'-' * 20}"

                await actions.send_group_forward_msg(
                    group_id=event.group_id,
                    message=Manager.Message(Segments.CustomNode(
                        str(event.self_id),
                        bot_name,
                        Manager.Message(Segments.Text(filter_sensitive_content(chat_summary)))
                    ))
                )
            else:
                chat_summary = generate_chat_summary(event.group_id)
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Reply(event.message_id),
                                                           Segments.Text(filter_sensitive_content(chat_summary))))

            nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
            chat_db = add_message(event.group_id, nike, user_message)
            return

        if event.group_id not in chat_db:
            pass

        nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
        chat_db = add_message(event.group_id, nike, user_message)

        if len(chat_db[event.group_id]['history']) % 10 == 0:
            try:
                os.makedirs(os.path.join(str(BASE_DIR), "data", 'sum_up'), exist_ok=True)
                json_path = os.path.join(str(BASE_DIR), "data", 'sum_up', 'chat_db.json')

                serializable = {}
                for gid, data in chat_db.items():
                    serializable[str(gid)] = {
                        "history": list(data["history"]),
                        "token_counter": int(data["token_counter"])
                    }
                tmp_path = json_path + ".tmp"
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(serializable, f, ensure_ascii=False)
                os.replace(tmp_path, json_path)
            except Exception:
                pass

        if "ping" == user_message:
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text("pong! 爆炸！v(◦'ωˉ◦)~♡ ")))
            return

        if is_feature_enabled("emoji_plus_one", True) and EMOJI_PLUS_ONE_ENABLED and has_emoji(user_message):
            if _is_emoji_plus_one_available(event.user_id, is_group=True, group_id=str(event.group_id)):
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(user_message)))
            return

        should_trigger = False
        order = ""
        is_wangkai_trigger = False
        is_at_trigger = False
        is_random_trigger = False

        if user_message.startswith(reminder):
            order_i = user_message.find(reminder)
            if order_i != -1:
                order = user_message[order_i + len(reminder):].strip()
                if order:
                    should_trigger = True
        else:
            has_at_bot = False
            text_content = ""

            for segment in event.message:
                if isinstance(segment, Segments.At) and int(segment.qq) == event.self_id:
                    has_at_bot = True
                elif isinstance(segment, Segments.Text):
                    text_content += segment.text + " "

            if has_at_bot:
                order = text_content.strip() if text_content.strip() else "用户艾特了你"
                should_trigger = True
                is_at_trigger = True
            elif any(trigger in user_message for trigger in ROBOT_NAME_TRIGGERS):
                has_text_wangkai = False
                text_content = ""

                for segment in event.message:
                    if isinstance(segment, Segments.Text) and any(trigger in segment.text for trigger in ROBOT_NAME_TRIGGERS):
                        has_text_wangkai = True
                        text_content += segment.text + " "

                if has_text_wangkai:
                    order = text_content.strip()
                    should_trigger = True
                    is_wangkai_trigger = True
            elif should_trigger_random_group_chat(user_message):
                order = user_message.strip()
                should_trigger = True
                is_random_trigger = True

        # 命令一律精确匹配（见 match_command / match_command_prefix），
        # 所以 @机器人 / 名字触发 等无前缀路径也能正常用命令，闲聊不会误命中。
        if is_feature_enabled("compression_commands", True) and await handle_compression_commands(event, actions, is_group=True, order=order):
            nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
            chat_db = add_message(event.group_id, nike, user_message)
            return

        if await handle_config_command(event, actions, is_group=True, order=order):
            nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
            chat_db = add_message(event.group_id, nike, user_message)
            return

        # 插件管理命令（群聊）
        if is_feature_enabled("plugin_admin_commands", False) and user_message.startswith(reminder):
            if f"{reminder}重载插件" == user_message and str(event.user_id) in ADMINS:
                global plugins, loaded_plugins, disabled_plugins, failed_plugins, plugins_help
                plugins = load_plugins()
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Text(f"✅ 插件重载完成，当前 {len(loaded_plugins)} 个插件已加载")))
                return
            elif user_message.startswith(f"{reminder}禁用插件 ") and str(event.user_id) in ADMINS:
                parts = user_message.split("禁用插件")
                if len(parts) > 1:
                    plugin_name = parts[-1].strip()
                    found_path = None
                    for ext in ["", ".py", ".pyw"]:
                        path = os.path.join(PLUGIN_FOLDER, plugin_name + ext)
                        if os.path.exists(path):
                            found_path = path
                            break
                    if not found_path:
                        dir_path = os.path.join(PLUGIN_FOLDER, plugin_name)
                        if os.path.isdir(dir_path):
                            found_path = dir_path
                    if found_path:
                        dirname, basename = os.path.split(found_path)
                        new_name = "d_" + basename
                        new_path = os.path.join(dirname, new_name)
                        try:
                            os.rename(found_path, new_path)
                            plugins = load_plugins()
                            await actions.send(group_id=event.group_id,
                                               message=Manager.Message(Segments.Text(f"✅ 插件 {plugin_name} 已禁用")))
                        except Exception as e:
                            await actions.send(group_id=event.group_id,
                                               message=Manager.Message(Segments.Text(f"❌ 禁用失败: {e}")))
                    else:
                        await actions.send(group_id=event.group_id,
                                           message=Manager.Message(Segments.Text(f"❌ 找不到插件 {plugin_name}")))
                else:
                    await actions.send(group_id=event.group_id,
                                       message=Manager.Message(Segments.Text("格式错误，请使用：{reminder}禁用插件 插件名")))
                return
            elif user_message.startswith(f"{reminder}启用插件 ") and str(event.user_id) in ADMINS:
                parts = user_message.split("启用插件")
                if len(parts) > 1:
                    plugin_name = parts[-1].strip()
                    found_path = None
                    for ext in ["", ".py", ".pyw"]:
                        path = os.path.join(PLUGIN_FOLDER, "d_" + plugin_name + ext)
                        if os.path.exists(path):
                            found_path = path
                            break
                    if not found_path:
                        dir_path = os.path.join(PLUGIN_FOLDER, "d_" + plugin_name)
                        if os.path.isdir(dir_path):
                            found_path = dir_path
                    if found_path:
                        dirname, basename = os.path.split(found_path)
                        original_name = basename[2:]
                        original_path = os.path.join(dirname, original_name)
                        try:
                            os.rename(found_path, original_path)
                            plugins = load_plugins()
                            await actions.send(group_id=event.group_id,
                                               message=Manager.Message(Segments.Text(f"✅ 插件 {plugin_name} 已启用")))
                        except Exception as e:
                            await actions.send(group_id=event.group_id,
                                               message=Manager.Message(Segments.Text(f"❌ 启用失败: {e}")))
                    else:
                        await actions.send(group_id=event.group_id,
                                           message=Manager.Message(Segments.Text(f"❌ 找不到已禁用的插件 {plugin_name}")))
                else:
                    await actions.send(group_id=event.group_id,
                                       message=Manager.Message(Segments.Text("格式错误，请使用：{reminder}启用插件 插件名")))
                return
            elif f"{reminder}插件视角" == user_message:
                status = f"""🔌 插件视角
——————————————
✅ 已加载插件 ({len(loaded_plugins)}):
{chr(10).join(f"{i+1}. {str(plugin).rsplit('_', 1)[0]}" for i, plugin in enumerate(loaded_plugins)) if loaded_plugins else "无"}

❌ 已禁用插件 ({len(disabled_plugins)}):
{chr(10).join(f"{i+1}. {plugin}" for i, plugin in enumerate(disabled_plugins)) if disabled_plugins else "无"}

⚠️ 加载失败 ({len(failed_plugins)}):
{chr(10).join(f"{i+1}. {plugin}" for i, plugin in enumerate(failed_plugins)) if failed_plugins else "无"}"""
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(status)))
                return


            elif f"{reminder}model" == user_message and str(event.user_id) in ADMINS:
                status_list = key_manager.get_status_list()
                lines = ["🤖 当前 API / Model 列表", "——————————————"]
                lines.append(f"🎯 当前使用: {key_manager.get_current_display()}")
                lines.append("")
                if not status_list:
                    lines.append("暂无可用配置")
                else:
                    for item in status_list:
                        flag_text = " <- 当前" if item["is_current"] else ""
                        last_error = item["last_error"][:80] if item["last_error"] else "无"
                        lines.append(
                            f"{item['id']}. {item.get('display_model') or item['model']}{flag_text}\n"
                            f"   地址: {item['base_url']}\n"
                            f"   Key: {item['key']}\n"
                            f"   状态: {item['status']}\n"
                            f"   失败次数: {item['fail_count']}\n"
                            f"   最近错误: {last_error}"
                        )

                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(Segments.Text("\n".join(lines)))
                )
                return

            elif user_message.startswith(f"{reminder}model ") and str(event.user_id) in ADMINS:
                target = user_message[len(f"{reminder}model "):].strip()
                ok = False
                if target.isdigit():
                    ok = key_manager.manual_switch_by_index(int(target))
                else:
                    ok = key_manager.manual_switch_by_model(target)
                if ok:
                    current_info = key_manager.get_current_display()
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Text(f"✅ 已切换成功\n当前: {current_info}"))
                    )
                else:
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Text(f"❌ 切换失败，未找到可用目标：{target}"))
                    )
                return

            elif f"{reminder}modellog" == user_message and str(event.user_id) in ADMINS:
                logs = key_manager.get_switch_logs(20)
                if not logs:
                    content = "📜 暂无 API 切换日志"
                else:
                    lines = ["📜 最近 API 切换日志", "——————————————"]
                    for log in logs:
                        mode = "手动" if log["manual"] else "自动"
                        lines.append(
                            f"[{log['time']}] {mode} {log['from']} -> {log['to']} | {log['reason']}"
                        )
                    content = "\n".join(lines)
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(Segments.Text(content))
                )
                return

            elif user_message.startswith(f"{reminder}启用model ") and str(event.user_id) in ADMINS:
                target = user_message[len(f"{reminder}启用model "):].strip()
                if target.isdigit() and key_manager.enable_key(int(target)):
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Text(f"✅ 已启用 model #{target}"))
                    )
                else:
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Text("❌ 启用失败，请检查编号"))
                    )
                return

            elif user_message.startswith(f"{reminder}重置model冷却 ") and str(event.user_id) in ADMINS:
                target = user_message[len(f"{reminder}重置model冷却 "):].strip()
                if target.isdigit() and key_manager.reset_cooldown(int(target)):
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Text(f"✅ 已重置 model #{target} 冷却状态"))
                    )
                else:
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Text("❌ 重置失败，请检查编号"))
                    )
                return

        if f"{reminder}重启" == user_message:
            if is_admin_user(event.user_id):
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Text(f"正在保存所有记忆并重启... 🧠💾")))

                try:
                    save_restart_state("group", event.group_id)

                except:
                    pass

                try:
                    stop_webui()
                except Exception:
                    pass
                Listener.restart()
            else:
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Text(
                                       f"仅管理员可操作")))
            return

        if match_command(order, "感知"):
            if is_admin_user(event.user_id):
                system_info = get_system_info()
                sessions = chat_memory.get_all_sessions()
                feel = f'''{bot_name} {bot_name_en} - 群聊模式
        ——————————————
        System Now
        运行时间: {seconds_to_hms(round(time.time() - second_start, 2))}
        系统版本: {system_info["version_info"]}
        CPU使用: {str(system_info["cpu_usage"]) + "%"}
        内存使用: {str(system_info["memory_usage_percentage"]) + "%"}
        ——————————————
        记忆存储
        私聊记忆: {len(sessions['private'])}个
        群聊记忆: {len(sessions['group'])}个
        压缩次数: {sum(cmc.compressor.compression_count.values())}次
        Token总计: {token_stats.total_tokens} Token（过去24小时）
        系统提示词: ✅ 独立存储'''
                for i, usage in enumerate(system_info["gpu_usage"]):
                    feel += f"\nGPU {i} 使用: {usage * 100:.2f}%"
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(feel)))
            else:
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Text("仅管理员可操作")))
            return

        if match_command(order, "管理员"):
            if is_admin_user(event.user_id):
                content = f'''管理我们的{bot_name}
——————————————
你拥有管理{bot_name}的权限。
    1. {reminder}设置 —— 查看/修改安全子集配置
    2. {reminder}立即压缩 —— 手动压缩本群对话
    3. {reminder}自动压缩 [开启/关闭] [阈值] —— 设置自动压缩
    4. {reminder}查看时间线 —— 查看本群对话时间线结构
    5. {reminder}查看记忆列表 —— 查看所有已存储的记忆
    6. {reminder}清除记忆 —— 清除本群对话记忆
    7. {reminder}压缩状态 —— 查看本群对话压缩状态
    8. {reminder}token统计 —— 查看过去24小时 Token 消耗
    9. {reminder}重置token统计 —— 清空过去24小时 Token 统计
    10. {reminder}重载插件 —— 重新加载所有插件
    11. {reminder}禁用插件 <插件名> —— 禁用指定插件
    12. {reminder}启用插件 <插件名> —— 启用指定插件
    13. {reminder}插件视角 —— 查看插件列表
    14. {reminder}model —— 查看所有 API / 模型状态
    15. {reminder}model <编号|模型名> —— 手动切换 API / 模型
    16. {reminder}modellog —— 查看最近 API 切换日志
    17. {reminder}启用model <编号> —— 手动恢复被禁用的 API
    18. {reminder}重置model冷却 <编号> —— 清除某个 API 的冷却状态
你的每一步操作，与用户息息相关。'''
            else:
                content = "仅管理员可操作"

            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(content)))
            return

        if user_message in (f"{reminder}帮助", f"{reminder}help"):
            content = f'''命令菜单
——————————————
{reminder}名言 —— 把引用消息生成名言图
{reminder}大头照 [@某人] —— 获取头像大图
{reminder}总结以上N条消息 —— 总结群聊消息
{reminder}聊天数据看板 —— 查看当前群统计
/reset 或 重置 —— 清除当前群对话
{reminder}设置 —— 查看可设置项
{reminder}设置 项目 值 —— 修改配置（仅管理员）
{reminder}压缩状态 —— 查看压缩状态
{reminder}立即压缩 —— 立刻压缩（仅管理员）
{reminder}自动压缩 [开启/关闭] [阈值] —— 设置自动压缩（仅管理员）
{reminder}查看时间线 —— 查看时间线（仅管理员）
{reminder}查看记忆列表 —— 查看记忆列表（仅管理员）
{reminder}清除记忆 —— 清除记忆（仅管理员）
{reminder}token统计 —— 查看过去24小时 Token 统计
{reminder}重置token统计 —— 清空过去24小时 Token 统计（仅管理员）
{reminder}感知 —— 查看运行状态（仅管理员）
{reminder}重载插件 / 禁用插件 / 启用插件 / 插件视角 —— 插件管理（仅管理员）
{reminder}model / modellog —— 模型管理（仅管理员）
{reminder}启用model / 重置model冷却 —— 恢复或清除冷却（仅管理员）
{reminder}重启 —— 重启机器人（仅管理员）'''
            content += build_plugins_help_section()
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(content)))
            return

        if match_command(order, "关于"):
            about = f'''{bot_name} {bot_name_en} - {project_name}
——————————————
Build Information
Version：{version_name}
Rebuilt from HypeR
'''
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(about)))
            return


        if match_command_prefix(order, "大头照"):
            uin = ""
            for i in event.message:
                if isinstance(i, Segments.At):
                    uin = i.qq
            if uin == "":
                uin = event.user_id
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(
                                   Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640")))
            return


        if should_trigger and order:
            # 在AI回复前，执行普通插件（非Any）
            plugin_context = base_plugin_context.copy()
            plugin_context.update({
                "event": event,
                "actions": actions,
                "user_id": event.user_id,
                "group_id": event.group_id,
                "user_message": user_message,
                "order": order,
                "is_group": True,
                "is_at_trigger": is_at_trigger,
                "is_wangkai_trigger": is_wangkai_trigger,
                "is_random_trigger": is_random_trigger,
            })
            if is_feature_enabled("plugins_external", True) and await execute_plugins(False, **plugin_context):
                return

            if not (is_feature_enabled("ai_chat", True) and is_feature_enabled("group_chat", True)):
                return

            try:
                text_content = ""
                image_urls = extract_image_urls_from_message(event.message)
                has_images = bool(image_urls)
                for i in event.message:
                    if isinstance(i, Segments.Text):
                        if (is_wangkai_trigger or is_at_trigger) and not user_message.startswith(reminder):
                            text_content += i.text + " "
                        else:
                            text_content += i.text.replace(reminder, "", 1) + " "
                    elif isinstance(i, Segments.Image):
                        pass

                final_message = build_group_ai_text_message(event_user_nickname, text_content.strip(), is_at_trigger=is_at_trigger)

                deepseek_context = cmc.get_context(event.user_id, event.group_id, event_user_nickname)

                result, total_tokens, prompt_tokens, completion_tokens = await deepseek_context.agen_content({"text": final_message, "image_urls": image_urls[:1]})
                result = result.rstrip("\n")

                # 开启群聊 AI 回复首条引用：普通命令、@触发、机器人名字触发均引用触发消息。
                # process_and_send 内部仍会检查：仅群聊普通消息且存在 message_id 时才真正引用，
                # 拍一拍等 NotifyEvent 不会引用，避免无 message_id 报错。
                reply_to_first = is_group_random_reply_quote_enabled() if is_random_trigger else True
                await process_and_send(actions, event, result, is_group=True, reply_to_first=reply_to_first,
                                      trace_id=getattr(deepseek_context, "last_trace_id", ""))


            except Exception as e:
                traceback.print_exc()
                await send_error_detail(
                    actions,
                    event,
                    e,
                    is_group=True,
                    reply=not (is_wangkai_trigger or is_at_trigger or is_random_trigger),
                    error_type="ai"
                )
            return


def run_with_retry():
    """运行机器人，断线后无限自动重连。"""
    global running
    retry_count = 0
    connect_time = 0

    def reconnect_delay(count: int) -> int:
        return 5 if count <= 5 else 10

    print(f"=== {bot_name} {bot_name_en} 启动中 ===")
    print(f"记忆存储: data/ai_memory/")
    print(f"动态压缩: 保留最近{user_cfg.get('compression_keep_recent', 20)}条消息，触发阈值{user_cfg.get('compression_threshold', 40)}条")
    print("=" * 20)
    set_connection_status("starting", "正在启动", "准备建立 OneBot / Hyper 连接")

    while running:
        try:
            # 每次重连前清空 Hyper reports 字典，防止断线期间积累的悬空 echo key 占用内存
            try:
                Manager.reports.contents.clear()
            except Exception:
                pass
            print(f"尝试启动机器人... (第{retry_count + 1}次尝试)")
            set_connection_status("connecting", "连接中", f"第 {retry_count + 1} 次尝试连接 OneBot / Hyper")
            connect_time = time.time()
            Listener.run()
            if HOT_SWITCH_IN_PROGRESS.is_set():
                HOT_SWITCH_IN_PROGRESS.clear()
                retry_count = 0
                print("♻️ 连接热切换已触发，立即按新配置重新建立连接...")
                continue
            if running:
                # 稳定运行超过30秒视为正常断开，重置重试计数
                if time.time() - connect_time > 30:
                    retry_count = 0
                else:
                    retry_count += 1
                wait_time = reconnect_delay(max(retry_count, 1))
                clear_current_qq_actions()
                set_connection_status("disconnected", "已断开", f"监听已退出，{wait_time} 秒后自动重连")
                if running:
                    print(f"连接断开，等待 {wait_time} 秒后重连...")
                    print("-" * 30)
                    time.sleep(wait_time)

        except KeyboardInterrupt:
            print(f"\n{bot_name} 收到退出信号")
            clear_current_qq_actions()
            set_connection_status("stopped", "已停止", "收到手动退出信号")
            running = False
            break

        except Exception as e:
            if HOT_SWITCH_IN_PROGRESS.is_set():
                hot_error = str(e)
                if "socket is already closed" in hot_error.lower() or "closed" in hot_error.lower():
                    HOT_SWITCH_IN_PROGRESS.clear()
                    retry_count = 0
                    print("♻️ 热切换期间旧连接已关闭，忽略本次预期异常并立即重连新地址...")
                    continue

            # 稳定运行超过30秒视为正常断开，重置重试计数
            if time.time() - connect_time > 30:
                retry_count = 0
            retry_count += 1
            error_msg = str(e)
            clear_current_qq_actions()
            set_connection_status("failed", "连接失败", error_msg)

            if "napcat" in error_msg.lower() or "连接" in error_msg or "连接失败" in error_msg:
                print(f"NapCat连接失败: {error_msg}")
            else:
                print(f"启动失败: {error_msg}")
                traceback.print_exc()

            if running:
                wait_time = reconnect_delay(retry_count)
                clear_current_qq_actions()
                set_connection_status("failed", "连接失败", f"{error_msg}；{wait_time} 秒后自动重连")
                print(f"等待 {wait_time} 秒后重连...")
                print("-" * 30)
                time.sleep(wait_time)
                continue

    print("机器人已停止运行")
    if not running:
        clear_current_qq_actions()
        set_connection_status("stopped", "已停止", "机器人已停止运行")


def restart_current_process(reason: str = "配置变更"):
    """重启当前 Python 进程，使连接配置百分百按最新值生效。"""
    try:
        print(f"🔁 准备重启主进程：{reason}")
        set_connection_status("connecting", "重启中", reason)
    except Exception:
        pass

    try:
        save_all_ai_memories()
        save_summary_records()
    except Exception:
        pass

    try:
        if 'cmc' in globals() and hasattr(cmc, 'compressor'):
            save_compression_stats(cmc.compressor)
    except Exception:
        pass

    try:
        stop_webui()
    except Exception:
        pass

    # os.execv 不触发 atexit，必须手动释放目录锁，否则新进程抢不到锁直接退出
    try:
        release_lock()
    except Exception as _e:
        print(f"重启前释放锁失败（忽略继续）：{_e}")

    python_exe = sys.executable
    argv = [python_exe] + sys.argv
    os.execv(python_exe, argv)



# ==================== 初始化聊天记忆管理器 ====================
chat_memory = ChatMemoryManager()

print("=" * 60)
print("🚀 初始化增强版上下文管理器")
print("=" * 60)

cmc = EnhancedContextManager()

# 初始化压缩统计
init_compression_stats()

# 扫描并显示已存储的记忆
sessions = chat_memory.get_all_sessions()
print(f"📊 已存储的记忆: {len(sessions['private'])}个私聊, {len(sessions['group'])}个群聊")
print(f"📊 全局Token统计: {token_stats.total_tokens} Token（过去24小时）")
print(f"⚙️ 系统提示词独立存储: ✅ 已启用")
print("=" * 60)

# ==================== 加载插件 ====================
print("=" * 60)
print("🔌 正在检查外部插件加载状态...")
plugins = load_plugins() if is_feature_enabled("plugins_external", True) else []
if not is_feature_enabled("plugins_external", True):
    print("ℹ️ 外部插件加载已关闭，当前仅使用内置功能开关")
print(f"📦 插件帮助信息已收集: {len(plugins_help)} 字符")
print("=" * 60)

# 注册退出保存
atexit.register(save_all_ai_memories)
atexit.register(lambda: save_compression_stats(cmc.compressor if 'cmc' in globals() else None))
atexit.register(lambda: print("🔄 正在保存所有AI记忆..."))

# ==================== 程序入口 ====================
if __name__ == "__main__":
    try:
        cleanup_legacy_config_files()
        Read_Settings()
        # 自动更新重启前释放目录锁，否则新进程会被 my_bot.lock 挡掉
        if callable(_set_pre_restart_callback):
            try:
                _set_pre_restart_callback(release_lock)
            except Exception as _e:
                print(f"注册自动更新重启回调失败（忽略）：{_e}")
        if callable(_set_qq_send_callback):
            try:
                _set_qq_send_callback(send_qq_message_from_http)
            except Exception as _e:
                print(f"注册 QQ 发送回调失败（忽略）：{_e}")
        if callable(_set_debug_self_message_callback):
            try:
                _set_debug_self_message_callback(send_debug_self_message)
            except Exception as _e:
                print(f"注册调试自检回调失败（忽略）：{_e}")
        start_webui(on_config_saved=apply_runtime_config)
        run_with_retry()
    except KeyboardInterrupt:
        stop_webui()
        print(f"\n{bot_name} 已手动停止")
        # 关闭所有AI客户端连接
        try:
            if 'cmc' in globals():
                for ctx in cmc.private_chats.values():
                    ctx._close_clients()
                for ctx in cmc.groups.values():
                    ctx._close_clients()
        except:
            pass
        save_summary_records()
        save_compression_stats(cmc.compressor if 'cmc' in globals() else None)
        print("✅ 所有记忆已保存")
    except Exception as e:
        stop_webui()
        print(f"程序异常: {e}")
        traceback.print_exc()
        # 关闭所有AI客户端连接
        try:
            if 'cmc' in globals():
                for ctx in cmc.private_chats.values():
                    ctx._close_clients()
                for ctx in cmc.groups.values():
                    ctx._close_clients()
        except:
            pass
        save_summary_records()
        save_compression_stats(cmc.compressor if 'cmc' in globals() else None)
        print("5秒后重新启动...")
        time.sleep(5)
        if callable(_set_qq_send_callback):
            try:
                _set_qq_send_callback(send_qq_message_from_http)
            except Exception as _e:
                print(f"注册 QQ 发送回调失败（忽略）：{_e}")
        if callable(_set_debug_self_message_callback):
            try:
                _set_debug_self_message_callback(send_debug_self_message)
            except Exception as _e:
                print(f"注册调试自检回调失败（忽略）：{_e}")
        start_webui(on_config_saved=apply_runtime_config)
        run_with_retry()