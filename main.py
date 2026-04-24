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
import GPUtil
from typing import Set, Dict, Optional
from collections import defaultdict, deque, Counter
from openai import OpenAI
from datetime import date
import atexit
import signal
import sys
import importlib

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ==================== 先初始化配置，但不破坏NapCat连接 ====================
from Hyper import Configurator

# 初始化配置管理器
CONFIG_FILE = "config.json"
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file=CONFIG_FILE).load_from_file())
config = Configurator.cm.get_cfg()

# ==================== 导入 key_manager ====================
from key_manager import key_manager

# ==================== 然后再导入其他 Hyper 模块 ====================
from Hyper import Listener, Events, Logger, Manager, Segments
from Hyper.Utils import Logic
from Hyper.Events import *

# ==================== 自定义模块导入 ====================
import Quote
from webui import start_webui, stop_webui, DEFAULT_FEATURE_SWITCHES, set_connection_status


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
        "api_failure_cooldown_seconds": others.get("api_failure_cooldown_seconds", 5),
        "api_default_index": others.get("api_default_index", 1),
        "api_default_model": others.get("api_default_model", ""),
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


def write_runtime_config(data: dict) -> bool:
    """统一写入唯一配置文件 config.json。"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.write("\n")
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
                switches[key] = bool(raw.get(key))
    return switches


def is_feature_enabled(key: str, default: bool = True) -> bool:
    return bool(get_feature_switches().get(key, default))


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


def normalize_location_query(name: str) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    for suffix in ("特别行政区", "自治区", "自治州", "自治县", "省", "市", "区", "县", "镇", "乡"):
        if text.endswith(suffix.lower()):
            text = text[: -len(suffix)]
            break
    return text


def pick_best_weather_location(results: list, city_name: str) -> dict | None:
    if not isinstance(results, list) or not results:
        return None

    target = normalize_location_query(city_name)

    def exact_match(item: dict) -> bool:
        """只允许精确匹配，避免“南通”被关键词/模糊匹配到“通海”。"""
        name = normalize_location_query(item.get("name", ""))
        admin1 = normalize_location_query(item.get("admin1", ""))
        country = normalize_location_query(item.get("country", ""))
        country_code = str(item.get("country_code", "") or "").upper()

        exact_names = {name}
        if admin1 and name:
            exact_names.add(f"{admin1}{name}")
        if country and admin1 and name:
            exact_names.add(f"{country}{admin1}{name}")
        if country_code and admin1 and name:
            exact_names.add(f"{country_code.lower()}{admin1}{name}")
        return bool(target and target in exact_names)

    exact_results = [item for item in results if isinstance(item, dict) and exact_match(item)]
    if not exact_results:
        return None

    def score(item: dict) -> tuple:
        country = str(item.get("country_code", "") or "").upper()
        is_cn = 1 if country == "CN" else 0
        population = float(item.get("population") or 0)
        return (is_cn, population)

    return max(exact_results, key=score)


def normalize_llm_endpoints(endpoints_config) -> list:
    """清洗 WebUI 中配置的 OpenAI 兼容接口。"""
    if not isinstance(endpoints_config, list):
        return []

    def _looks_like_placeholder_key(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return True
        lowered = text.lower()
        placeholder_tokens = [
            "your_api_key",
            "api_key",
            "sk-xxxx",
            "sk-***",
            "在这里填写",
            "这里填",
            "示例",
            "例子",
            "占位",
            "测试key",
            "测试 key",
        ]
        return any(token in lowered for token in placeholder_tokens)

    def _normalize_api_keys(keys_config) -> list[str]:
        if isinstance(keys_config, str):
            raw_keys = [x.strip() for x in keys_config.splitlines() if x.strip()]
        elif isinstance(keys_config, list):
            raw_keys = [str(x).strip() for x in keys_config if str(x).strip()]
        else:
            raw_keys = []

        normalized_keys = []
        for raw_key in raw_keys:
            key = raw_key.strip().strip('"').strip("'").strip()
            if not key or _looks_like_placeholder_key(key):
                continue
            try:
                key.encode("ascii")
            except UnicodeEncodeError:
                print(f"[API Key] 已忽略包含非 ASCII 字符的无效 Key: {key[:8]}...")
                continue
            normalized_keys.append(key)
        return normalized_keys

    normalized = []
    for raw_ep in endpoints_config:
        if not isinstance(raw_ep, dict):
            continue
        ep = dict(raw_ep)
        base_url = str(ep.get("base_url", "")).strip()
        if not base_url:
            continue

        model = str(ep.get("model", "")).strip()
        if not model:
            lower_url = base_url.lower()
            if "siliconflow" in lower_url:
                model = "deepseek-ai/DeepSeek-V3.2"
            elif "deepseek.com" in lower_url or "iflow.cn" in lower_url:
                model = "deepseek-chat"
            elif "localhost" in lower_url or "127.0.0.1" in lower_url:
                model = "deepseek-ai/DeepSeek-V3.2"
            else:
                model = "deepseek-chat"

        keys = _normalize_api_keys(ep.get("keys", []))

        if not keys:
            print(f"[API Key] 已跳过未配置有效 Key 的端点: {base_url} | model={model}")
            continue

        normalized.append({
            "base_url": base_url,
            "model": model,
            "keys": keys,
            "supports_multimodal": normalize_bool_config(ep.get("supports_multimodal", False), False),
        })

    return normalized


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

    candidates = [raw_url]
    http_url = replace_scheme_with_http(raw_url)
    if http_url and http_url != raw_url:
        candidates.append(http_url)

    timeout = aiohttp.ClientTimeout(total=20)
    last_error = None
    for candidate in candidates:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(candidate, ssl=False, headers={"User-Agent": "Mozilla/5.0"}) as response:
                    if response.status != 200:
                        last_error = f"HTTP {response.status}"
                        continue
                    data = await response.read()
                    if not data:
                        last_error = "empty body"
                        continue
                    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if not content_type or not content_type.startswith("image/"):
                        guessed, _ = mimetypes.guess_type(candidate)
                        content_type = guessed or "image/jpeg"
                    encoded = base64.b64encode(data).decode("utf-8")
                    return f"data:{content_type};base64,{encoded}"
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


def build_private_ai_text_message(event_user_nickname: str, text: str) -> str:
    return f"【{event_user_nickname}】说：{filter_sensitive_content(str(text or '').strip())}"


def build_group_ai_text_message(event_user_nickname: str, text: str, is_at_trigger: bool = False) -> str:
    cleaned = filter_sensitive_content(str(text or '').strip())
    if is_at_trigger and not cleaned:
        return f"【{event_user_nickname}】艾特了你"
    return f"【{event_user_nickname}】说：{cleaned}"


def apply_api_rotation_settings(cfg: dict = None, verbose: bool = True) -> list:
    """应用 LLM 接口列表与默认 API / 模型轮换设置。"""
    cfg = cfg or user_cfg
    endpoints_config = normalize_llm_endpoints(cfg.get("llm_endpoints", []))
    if not endpoints_config:
        endpoints_config = normalize_llm_endpoints(getattr(config, "others", {}).get("llm_endpoints", []))

    if not endpoints_config:
        if verbose:
            print("警告: 未配置任何 API 端点，AI 功能将不可用")
        key_manager.set_endpoints([])
        return []

    key_manager.set_endpoints(endpoints_config)

    default_model = str(cfg.get("api_default_model", "") or "").strip()
    default_index_raw = cfg.get("api_default_index", 1)
    default_applied = False
    if default_model:
        default_applied = key_manager.set_default_by_model(default_model)
    if not default_applied:
        try:
            default_index = int(default_index_raw) if str(default_index_raw).strip() else 1
        except (TypeError, ValueError):
            default_index = 1
        if default_index > 0 and key_manager.get_all_keys():
            default_applied = key_manager.set_default_by_index(default_index)
    if not default_applied and key_manager.get_all_keys():
        key_manager.set_default_by_index(1)

    if verbose:
        total_keys = sum(len(ep.get("keys", [])) for ep in endpoints_config)
        print(f"已加载 {total_keys} 个 API Key，将自动轮换")
        print(f"默认接口: {key_manager.get_default_display()}")
        print(f"当前接口: {key_manager.get_current_display()}")
        print("API 端点列表：")
        for i, ep in enumerate(endpoints_config, start=1):
            print(f"  [{i}] 模型: {ep['model']} | 地址: {ep['base_url']} | Key 数量: {len(ep.get('keys', []))}")

    return endpoints_config


def close_runtime_llm_clients():
    """关闭已缓存的 OpenAI 客户端，确保 WebUI 修改 API/超时等配置后立即使用新连接。"""
    try:
        manager = globals().get("cmc")
        if manager is not None:
            for ctx in list(getattr(manager, "private_chats", {}).values()) + list(getattr(manager, "groups", {}).values()):
                try:
                    if hasattr(ctx, "_close_clients"):
                        ctx._close_clients()
                except Exception:
                    pass
            compressor = getattr(manager, "compressor", None)
            if compressor is not None and hasattr(compressor, "_close_clients"):
                try:
                    compressor._close_clients()
                except Exception:
                    pass
    except Exception as e:
        print(f"关闭旧 LLM 客户端缓存失败: {e}")


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
        ROOT_User = user_cfg.get("root_users", [])
        if "load_admin_lists_from_config" in globals():
            Super_User, Manage_User = load_admin_lists_from_config()

        POKE_COOLDOWN_SECONDS = normalize_seconds_config(user_cfg.get("poke_cooldown_seconds", 8), default=8.0)
        POKE_REPLY_ENABLED = bool(user_cfg.get("poke_reply_enabled", True))
        EMOJI_PLUS_ONE_ENABLED = bool(user_cfg.get("emoji_plus_one_enabled", True))
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

        if is_feature_enabled("plugins_external", False):
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



#==================== 跨平台进程锁适配 (已修复) ====================
LOCK_FILE = None
lock_fp = None

if sys.platform == 'win32':
    # Windows 方案：使用 msvcrt (内置模块，无需安装)
    import msvcrt
    LOCK_FILE = os.path.join(os.getcwd(), 'my_bot.lock') # 锁文件放在当前目录
    try:
        lock_fp = open(LOCK_FILE, 'w')
        # 尝试获取独占锁，失败则抛出 OSError
        msvcrt.locking(lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
        print(f"✅ 进程锁获取成功 (Windows), PID: {os.getpid()}")
    except OSError:
        print("❌ 另一个实例已在运行 (Windows)，退出")
        sys.exit(1)
else:
    # Linux/Mac 方案：使用 fcntl
    import fcntl
    LOCK_FILE = '/tmp/my_bot.lock'
    try:
        lock_fp = open(LOCK_FILE, 'w')
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(f"✅ 进程锁获取成功 (Linux), PID: {os.getpid()}")
    except IOError:
        print("❌ 另一个实例已在运行 (Linux)，退出")
        sys.exit(1)

def release_lock():
    global lock_fp, LOCK_FILE
    try:
        if lock_fp:
            if sys.platform == 'win32':
                # Windows 关闭文件即释放锁
                lock_fp.close()
                if os.path.exists(LOCK_FILE):
                    try:
                        os.remove(LOCK_FILE)
                    except:
                        pass
            else:
                # Linux 需要显式解锁
                import fcntl
                fcntl.flock(lock_fp, fcntl.LOCK_UN)
                lock_fp.close()
                if os.path.exists(LOCK_FILE):
                    try:
                        os.unlink(LOCK_FILE)
                    except:
                        pass
            print("✅ 进程锁已释放")
    except Exception as e:
        print(f"释放锁时出错：{e}")

atexit.register(release_lock)
# ==================== 全局变量 ====================
cooldowns = {}
poke_cooldowns = {}
POKE_COOLDOWN_SECONDS = normalize_seconds_config(user_cfg.get("poke_cooldown_seconds", 8), default=8.0)
POKE_REPLY_ENABLED = bool(user_cfg.get("poke_reply_enabled", True))
EMOJI_PLUS_ONE_ENABLED = bool(user_cfg.get("emoji_plus_one_enabled", True))
EMOJI_PLUS_ONE_COOLDOWN_SECONDS = float(user_cfg.get("emoji_plus_one_cooldown_seconds", 1.0))
second_start = time.time()
EnableNetwork = "Pixmap"
user_lists = {}
settings_loaded = False
emoji_send_count: datetime = None
generating = False
running = True  # 添加运行标志

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
            "default_enabled": bool(data.get("default_enabled", True)),
            "groups": {str(k): bool(v) for k, v in groups.items()}
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


def can_summary_today(group_id: str) -> tuple[bool, str]:
    """检查群聊今天是否还可以总结"""
    today = date.today().isoformat()
    today_count = daily_summary_records[group_id][today]

    if today_count >= SUMMARY_PER_DAY_LIMIT:
        return False, f"❌ 本群今天已经总结过了，请明天再试 (｡•́︿•̀｡)"

    return True, f"还可以总结，今天已总结 {today_count} 次"


def record_summary(group_id: str):
    """记录群聊的一次总结"""
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
        os.makedirs(os.path.join("data", 'sum_up'), exist_ok=True)
        records_path = os.path.join("data", 'sum_up', 'summary_records.json')

        serializable_records = {}
        for group_id, dates in daily_summary_records.items():
            serializable_records[str(group_id)] = dict(dates)

        with open(records_path, 'w', encoding='utf-8') as f:
            json.dump(serializable_records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存总结记录失败: {e}")


def load_summary_records():
    """从文件加载总结记录"""
    global daily_summary_records
    try:
        records_path = os.path.join("data", 'sum_up', 'summary_records.json')
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
os.makedirs(os.path.join("data", 'sum_up'), exist_ok=True)
os.makedirs(os.path.join("data", 'ai_memory'), exist_ok=True)
os.makedirs(os.path.join("data", 'compression'), exist_ok=True)
os.makedirs("./temps", exist_ok=True)
os.makedirs("Tools", exist_ok=True)


# ==================== 聊天数据库（兼容旧代码）====================
def default_factory():
    return {
        "history": deque(maxlen=1000),
        "token_counter": 0
    }


def load_chat_db():
    """加载聊天数据库 - 兼容旧版总结功能"""
    chat_db = defaultdict(default_factory)
    pkl_path = os.path.join("data", 'sum_up', 'chat_db.pkl')

    if os.path.exists(pkl_path) and os.path.getsize(pkl_path) > 0:
        try:
            with open(pkl_path, 'rb') as f:
                loaded_db = pickle.load(f)
                if isinstance(loaded_db, dict):
                    for group_id, data in loaded_db.items():
                        history_list = data.get("history", [])
                        token_counter = int(data.get("token_counter", 0))
                        chat_db[group_id]["history"] = deque(history_list, maxlen=1000)
                        chat_db[group_id]["token_counter"] = token_counter
        except Exception as e:
            print(f"SumUp: 加载历史消息失败: {e}")

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
    """只从 config.json 读取管理列表，避免与本地 ini 文件冲突。"""
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
    manage_users = []
    seen = set()
    for item in owner_users + root_users:
        if item and item not in seen:
            seen.add(item)
            manage_users.append(item)
    super_users = manage_users[:]
    return super_users, manage_users


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
    global Super_User, Manage_User
    cleanup_legacy_config_files()
    Super_User, Manage_User = load_admin_lists_from_config()


def Write_Settings(s: list, m: list) -> bool:
    """写入权限设置到 config.json。"""
    s = [item for item in s if item]
    m = [item for item in m if item]
    global Super_User, Manage_User

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

        Super_User = manage_users[:]
        Manage_User = manage_users
        return True
    except Exception as e:
        print(f"写入 config 管理列表失败: {e}")
        return False


# ==================== 工具函数 ====================
def seconds_to_hms(total_seconds):
    """秒转换为时分秒"""
    hours = total_seconds // 3600
    remaining_seconds = total_seconds % 3600
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60
    return f"{hours}h, {minutes}m, {seconds}s"


def has_emoji(s: str) -> bool:
    """检查是否只有一个表情符号"""
    return emoji.emoji_count(s) == 1 and len(s) == 1


def estimate_tokens(text: str) -> int:
    """估算Token数量（后备方案）"""
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    non_chinese = len(text) - chinese_chars
    return chinese_chars + (non_chinese // 4) + 1


def get_system_info():
    """获取系统信息"""
    version_info = platform.platform()
    architecture = platform.architecture()
    cpu_usage = psutil.cpu_percent(interval=1)
    virtual_memory = psutil.virtual_memory()
    memory_usage_percentage = virtual_memory.percent

    gpus = GPUtil.getGPUs()
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
    plain_text = extract_plain_text_from_message(event)

    if text.startswith(reminder) or plain_text.startswith(reminder):
        return True
    if is_at_bot_message(event):
        return True
    if any(plain_text.startswith(trigger) for trigger in ROBOT_NAME_TRIGGERS):
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


async def handle_check_account_command(event, actions, order, is_group=True):
    if not is_feature_enabled("check_account", True):
        return False
    if not order.startswith("开"):
        return False

    uid = 0
    for i in event.message:
        if isinstance(i, Segments.At):
            uid = int(i.qq)
            break

    if uid == 0:
        uid_str = order[order.find("开") + 1:].strip()
        if not uid_str:
            uid_str = str(event.user_id)
        try:
            uid = int(uid_str)
        except (ValueError, TypeError):
            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {uid_str} 不是一个有效的用户'''
            if is_group:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            else:
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(r)))
            return True

    ws_url = f"ws://{config.connection.host}:{config.connection.port}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                request_id = str(uuid.uuid4())
                payload = {
                    "action": "get_stranger_info",
                    "params": {"user_id": uid, "no_cache": True},
                    "echo": request_id,
                }
                await ws.send_str(json.dumps(payload))
                user_info = None
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        response_data = json.loads(msg.data)
                        if response_data.get("echo") == request_id:
                            user_info = response_data.get("data")
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
    except Exception as e:
        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 获取用户信息时出错: {e}'''
        if is_group:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
        else:
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(r)))
        return True

    if not user_info:
        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 未能获取到 {uid} 的信息，可能不是一个有效的用户。'''
        if is_group:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
        else:
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(r)))
        return True

    framework = await actions.get_version_info()
    framework = framework.data.raw
    if "NapCat" in framework.get("app_name"):
        avatar, r = parser_user_info_napcat(user_info, Super_User + ROOT_User + Manage_User, Super_User + ROOT_User, ROOT_User)
    else:
        avatar, r = parse_user_info(user_info, Super_User + ROOT_User + Manage_User, Super_User + ROOT_User, ROOT_User)

    message = Manager.Message(Segments.Image(avatar), Segments.Text(r)) if avatar else Manager.Message(Segments.Text(r))
    if is_group:
        await actions.send(group_id=event.group_id, message=message)
    else:
        await actions.send(user_id=event.user_id, message=message)
    return True


def parser_user_info_napcat(user_dict, ADMINS, SUPERS, ROOT_User):
    try:
        avatar = user_dict.get('avatar', '')
        register_time = user_dict.get('reg_time', '')
        try:
            dt = datetime.datetime.strptime(register_time, '%Y-%m-%dT%H:%M:%SZ')
            register_time = dt.strftime('%Y.%m.%d %H:%M:%S')
        except (ValueError, TypeError):
            register_time = '未知时间'

        is_vip = user_dict.get('is_vip', False)
        vip_level = user_dict.get('vip_level', 0)
        is_year_vip = user_dict.get('is_years_vip', False)

        status_msg = "(框架不支持)"
        if str(user_dict.get('user_id', '未知')) in ROOT_User:
            status_user = "ROOT_User"
        elif str(user_dict.get('user_id', '未知')) in SUPERS:
            status_user = "Super_User"
        elif str(user_dict.get('user_id', '未知')) in ADMINS:
            status_user = "Manage_User"
        else:
            status_user = "普通用户"

        result = f"""昵称: {user_dict.get('nickname', '未知')}
状态: {status_msg}
QQ号: {user_dict.get('uin', '未知')}
QID: {user_dict.get('qid', '未知')}
性别: {'男' if user_dict.get('sex') == 'male' else '女'}
年龄: {user_dict.get('age', '未知')}
权限: {status_user}
QQ等级: {user_dict.get('qqLevel', '未知')}
个性签名: {user_dict.get('longNick', '暂无签名')}
注册时间: {register_time}
超级会员: {'是' if is_vip else '否'}
会员等级: {vip_level}
年费会员: {'是' if is_year_vip else '否'}"""
        return avatar, result
    except Exception:
        print(f"解析失败: {traceback.format_exc()}")
        return "", "无法打开该用户的账户"


def parse_user_info(user_dict, ADMINS, SUPERS, ROOT_User):
    try:
        avatar = user_dict.get('avatar', '')
        register_time = user_dict.get('RegisterTime', '')
        try:
            dt = datetime.datetime.strptime(register_time, '%Y-%m-%dT%H:%M:%SZ')
            register_time = dt.strftime('%Y.%m.%d %H:%M:%S')
        except (ValueError, TypeError):
            register_time = '未知时间'

        business = user_dict.get('Business', [])
        is_vip = any(item.get('type') == 1 for item in business)
        vip_level = next((item.get('level', 0) for item in business if item.get('type') == 1), 0)
        is_year_vip = any(item.get('isyear') == 1 for item in business if item.get('type') == 1)

        status_msg = user_dict.get('status', {}).get('message', '暂无状态')
        if str(user_dict.get('user_id', '未知')) in ROOT_User:
            status_user = "ROOT_User"
        elif str(user_dict.get('user_id', '未知')) in SUPERS:
            status_user = "Super_User"
        elif str(user_dict.get('user_id', '未知')) in ADMINS:
            status_user = "Manage_User"
        else:
            status_user = "普通用户"

        result = f"""昵称: {user_dict.get('nickname', '未知')}
状态: {status_msg}
QQ号: {user_dict.get('user_id', '未知')}
QID: {user_dict.get('q_id', '未知')}
性别: {'男' if user_dict.get('sex') == 'male' else '女'}
年龄: {user_dict.get('age', '未知')}
权限: {status_user}
QQ等级: {user_dict.get('level', '未知')}
个性签名: {user_dict.get('sign', '暂无签名')}
注册时间: {register_time}
超级会员: {'是' if is_vip else '否'}
会员等级: {vip_level}
年费会员: {'是' if is_year_vip else '否'}"""
        return avatar, result
    except Exception:
        print(f"解析失败: {traceback.format_exc()}")
        return "", "无法打开该用户的账户"
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
        with open(RESTART_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
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
nickname_cache = {}

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
                    return filtered_name
        except:
            pass

    if cache_key in nickname_cache:
        name, timestamp = nickname_cache[cache_key]
        if time.time() - timestamp < 600:
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
                    return res
            except:
                pass 
        
        user_info = await asyncio.wait_for(actions.get_stranger_info(user_id), timeout=2.0)
        nickname = user_info.data.raw.get('nickname', str(user_id))
        res = filter_sensitive_content(nickname)
        nickname_cache[cache_key] = (res, time.time())
        return res
    except:
        return str(user_id)


class LimitedDeepSeekContext:
    """严格限制上下文消息数量的 DeepSeek 上下文 - 系统提示词独立存储"""

    def __init__(self, system_prompt: str):
        self.system_prompt = filter_sensitive_content(system_prompt)
        self.max_messages = int(user_cfg.get("context_max_messages", 60))
        self.history = []       # 这里只存 user/assistant 类型历史，不存系统提示词
        self._client_pool = {}  # (thread_id, base_url, key) -> client

    def _get_client(self, base_url: str, api_key: str):
        """获取或创建 OpenAI 客户端（支持不同端点）"""
        thread_id = threading.get_ident()
        cache_key = f"{thread_id}_{base_url}_{api_key}"
        if cache_key not in self._client_pool:
            self._client_pool[cache_key] = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=API_REQUEST_TIMEOUT_SECONDS + 5,
                max_retries=1
            )
        return self._client_pool[cache_key]

    def _close_clients(self):
        """关闭所有客户端连接"""
        for client in self._client_pool.values():
            try:
                if hasattr(client, 'close'):
                    client.close()
                if hasattr(client, '_client') and hasattr(client._client, 'close'):
                    client._client.close()
            except Exception:
                pass
        self._client_pool.clear()

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

    def _enforce_message_limit(self):
        """强制执行消息数量限制，仅裁剪普通历史"""
        try:
            if len(self.history) > self.max_messages:
                self.history = self.history[-self.max_messages:]
        except Exception:
            pass

    async def agen_content(self, message) -> tuple[str, int, int, int]:
        max_retries = len(key_manager.get_all_keys()) or 1
        last_error = None
        tried_keys = set()

        for attempt in range(max_retries):
            require_multimodal = isinstance(message, dict) and bool(message.get("image_urls"))
            current = key_manager.get_next_for_request(
                tried_keys=tried_keys,
                include_cooldown=True,
                require_multimodal=require_multimodal,
            )
            if not current:
                break

            base_url, current_key, model, supports_multimodal = current
            tried_keys.add(current_key)

            try:
                self._enforce_message_limit()
                image_urls = []
                if isinstance(message, dict):
                    user_content = str(message.get("text", "") or "")
                    image_urls = await prepare_image_inputs_for_model(
                        message.get("image_urls", []) or [],
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

                client = self._get_client(base_url, current_key)

                try:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.chat.completions.create,
                            model=model,
                            messages=messages,
                            stream=False,
                            timeout=API_REQUEST_TIMEOUT_SECONDS
                        ),
                        timeout=API_REQUEST_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    raise Exception(f"API 请求超过 {API_REQUEST_TIMEOUT_SECONDS} 秒未返回，已自动切换下一个")

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
                total_tokens = getattr(usage, "total_tokens", 0) if usage else 0
                prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

                self.history.append({
                    "role": "user",
                    "content": filter_sensitive_content(user_content)
                })
                self.history.append({
                    "role": "assistant",
                    "content": result
                })

                self._enforce_message_limit()
                key_manager.mark_success(current_key)

                return result, total_tokens, prompt_tokens, completion_tokens

            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}".lower()
                print(f"[DEBUG] API 调用失败 (key: {current_key[:8]}..., model: {model}): {e}")

                if "429" in error_msg or "rate limit" in error_msg or "rpm limit" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "503" in error_msg or "busy" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "500" in error_msg or "502" in error_msg or "504" in error_msg or "timeout" in error_msg or "403" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "invalid" in error_msg or "unauthorized" in error_msg or "401" in error_msg:
                    if key_manager.is_default_key(current_key):
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    else:
                        key_manager.disable_key(current_key, reason=str(e))
                    last_error = e
                    continue
                elif "model not exist" in error_msg or "not support" in error_msg or "404" in error_msg:
                    if key_manager.is_default_key(current_key):
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    else:
                        key_manager.disable_key(current_key, reason=str(e))
                    last_error = e
                    continue
                elif "quota" in error_msg or "insufficient" in error_msg or "balance" in error_msg or "402" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "choices" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "llm 回复命中切换关键词" in str(e).lower():
                    print(f"[LLM Failover] 回复命中关键词，切换下一个 API: model={model}, keyword={str(e)}")
                    key_manager.mark_failure(
                        current_key,
                        reason=str(e),
                        cooldown_seconds=get_api_failure_cooldown_seconds(),
                    )
                    last_error = e
                    continue
                else:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue

        raise last_error or Exception("所有 API Key 均失败")

    def clear(self):
        """清除上下文"""
        self.history.clear()
        self._close_clients()

    def add_message(self, role: str, content: str):
        """添加消息到历史，仅允许 user / assistant"""
        content = filter_sensitive_content(content)
        if role in ["user", "assistant"]:
            self.history.append({"role": role, "content": content})
        self._enforce_message_limit()

    def get_message_count(self):
        return len(self.history)

    def get_stats(self) -> dict:
        return {"total_tokens": 0, "total_calls": 0}

    def __del__(self):
        self._close_clients()

# ==================== 聊天记忆管理器 - 独立分类存储 ====================
class ChatMemoryManager:
    """聊天记忆管理器 - 每个会话独立存储，只存储对话历史，不存系统提示词"""

    def __init__(self):
        self.private_chats: Dict[int, dict] = {}
        self.group_chats: Dict[int, dict] = {}
        self.memory_path = os.path.join("data", 'ai_memory')

    def _get_private_filename(self, user_id: int) -> str:
        return os.path.join(self.memory_path, f'private_{user_id}.json')

    def _get_group_filename(self, group_id: int) -> str:
        return os.path.join(self.memory_path, f'group_{group_id}.json')

    def save_private_memory(self, user_id: int, history: list, token_counter: int = 0):
        """保存私聊记忆"""
        try:
            file_path = self._get_private_filename(user_id)
            clean_history = [msg for msg in history if msg.get('role') in ['user', 'assistant']]
            data = {
                'user_id': user_id,
                'history': clean_history,
                'token_counter': token_counter,
                'save_time': time.time(),
                'version': '2.1'
            }
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            return True
        except Exception as e:
            return False

    def load_private_memory(self, user_id: int) -> tuple[list, int]:
        """加载私聊记忆"""
        try:
            file_path = self._get_private_filename(user_id)
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                history = data.get('history', [])
                history = [msg for msg in history if msg.get('role') in ['user', 'assistant']]
                token_counter = data.get('token_counter', 0)
                return history, token_counter
        except Exception as e:
            pass
        return [], 0

    def save_group_memory(self, group_id: int, history: list, token_counter: int = 0, group_roles: dict = None):
        """保存群聊记忆"""
        try:
            file_path = self._get_group_filename(group_id)
            clean_history = [msg for msg in history if msg.get('role') in ['user', 'assistant']]
            data = {
                'group_id': group_id,
                'history': clean_history,
                'token_counter': token_counter,
                'group_roles': group_roles or {},
                'save_time': time.time(),
                'version': '2.1'
            }
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            return True
        except Exception as e:
            return False

    def load_group_memory(self, group_id: int) -> tuple[list, int, dict]:
        """加载群聊记忆"""
        try:
            file_path = self._get_group_filename(group_id)
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                history = data.get('history', [])
                history = [msg for msg in history if msg.get('role') in ['user', 'assistant']]
                token_counter = data.get('token_counter', 0)
                group_roles = data.get('group_roles', {})
                return history, token_counter, group_roles
        except Exception as e:
            pass
        return [], 0, {}

    def delete_private_memory(self, user_id: int):
        try:
            file_path = self._get_private_filename(user_id)
            if os.path.exists(file_path):
                os.remove(file_path)
                return True
        except Exception as e:
            pass
        return False

    def delete_group_memory(self, group_id: int):
        try:
            file_path = self._get_group_filename(group_id)
            if os.path.exists(file_path):
                os.remove(file_path)
                return True
        except Exception as e:
            pass
        return False

    def get_all_sessions(self) -> dict:
        sessions = {'private': [], 'group': []}
        try:
            for filename in os.listdir(self.memory_path):
                if filename.startswith('private_') and filename.endswith('.json'):
                    user_id = filename.replace('private_', '').replace('.json', '')
                    sessions['private'].append(int(user_id))
                elif filename.startswith('group_') and filename.endswith('.json'):
                    group_id = filename.replace('group_', '').replace('.json', '')
                    sessions['group'].append(int(group_id))
        except Exception as e:
            pass
        return sessions


class ContextCompressor:
    """对话上下文动态压缩器"""

    def __init__(self, compression_threshold: int = 40):
        self.compression_threshold = compression_threshold
        self.keep_recent = int(user_cfg.get("compression_keep_recent", 20))
        self.compression_count = {}
        self.last_compression_time = {}
        self.max_sessions = 1000
        self._client_pool = {}  # (thread_id, base_url, key) -> client

    def _get_client(self, base_url: str, api_key: str):
        """获取或创建用于压缩摘要的 OpenAI 客户端。"""
        thread_id = threading.get_ident()
        cache_key = f"{thread_id}_{base_url}_{api_key}"
        if cache_key not in self._client_pool:
            self._client_pool[cache_key] = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=API_REQUEST_TIMEOUT_SECONDS + 5,
                max_retries=1
            )
        return self._client_pool[cache_key]

    def _close_clients(self):
        """关闭压缩摘要用到的客户端连接。"""
        for client in self._client_pool.values():
            try:
                if hasattr(client, 'close'):
                    client.close()
                if hasattr(client, '_client') and hasattr(client._client, 'close'):
                    client._client.close()
            except Exception:
                pass
        self._client_pool.clear()

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

            max_retries = len(key_manager.get_all_keys()) or 1
            last_error = None
            tried_keys = set()
            summary = ""

            for attempt in range(max_retries):
                current = key_manager.get_next_for_request(tried_keys=tried_keys, include_cooldown=True)
                if not current:
                    break

                base_url, current_key, model, supports_multimodal = current
                tried_keys.add(current_key)

                try:
                    client = self._get_client(base_url, current_key)
                    print(f"[DEBUG] 压缩摘要使用 API: model={model}, base_url={base_url}, key={current_key[:8]}...")

                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.chat.completions.create,
                            model=model,
                            messages=api_messages,
                            stream=False,
                            timeout=API_REQUEST_TIMEOUT_SECONDS
                        ),
                        timeout=API_REQUEST_TIMEOUT_SECONDS
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
                    key_manager.mark_success(current_key)
                    break

                except asyncio.TimeoutError:
                    e = Exception(f"压缩摘要 API 请求超过 {API_REQUEST_TIMEOUT_SECONDS} 秒未返回，已自动切换下一个")
                    error_msg = f"{type(e).__name__}: {e}".lower()
                    print(f"[DEBUG] 压缩摘要 API 调用超时 (key: {current_key[:8]}..., model: {model}): {e}")
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                except Exception as e:
                    error_msg = f"{type(e).__name__}: {e}".lower()
                    print(f"[DEBUG] 压缩摘要 API 调用失败 (key: {current_key[:8]}..., model: {model}): {e}")

                    if "429" in error_msg or "rate limit" in error_msg or "rpm limit" in error_msg:
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    elif "503" in error_msg or "busy" in error_msg:
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    elif "500" in error_msg or "502" in error_msg or "504" in error_msg or "timeout" in error_msg or "403" in error_msg:
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    elif "invalid" in error_msg or "unauthorized" in error_msg or "401" in error_msg:
                        if key_manager.is_default_key(current_key):
                            key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        else:
                            key_manager.disable_key(current_key, reason=str(e))
                    elif "model not exist" in error_msg or "not support" in error_msg or "404" in error_msg:
                        if key_manager.is_default_key(current_key):
                            key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                        else:
                            key_manager.disable_key(current_key, reason=str(e))
                    elif "quota" in error_msg or "insufficient" in error_msg or "balance" in error_msg or "402" in error_msg:
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    elif "choices" in error_msg:
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    else:
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())

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



# ==================== 全局Token统计管理器 ====================
class TokenStats:
    """真实的Token统计管理器"""

    def __init__(self):
        self.total_tokens = 0
        self.session_tokens = defaultdict(int)
        self.user_tokens = defaultdict(int)
        self.group_tokens = defaultdict(int)
        self.detailed_stats = defaultdict(list)
        self.last_update = time.time()

    def add_usage(self, session_id: str, user_id: int = None, group_id: int = None,
                  tokens: int = 0, prompt_tokens: int = 0, completion_tokens: int = 0,
                  model: str = "deepseek-chat"):
        """记录真实的Token使用情况"""
        if tokens <= 0:
            return

        self.total_tokens += tokens
        self.session_tokens[session_id] += tokens

        if user_id:
            self.user_tokens[str(user_id)] += tokens
        if group_id:
            self.group_tokens[str(group_id)] += tokens

        self.detailed_stats[session_id].append({
            'time': time.time(),
            'tokens': tokens,
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'model': model,
            'user_id': user_id,
            'group_id': group_id
        })

        if len(self.detailed_stats[session_id]) > 100:
            self.detailed_stats[session_id] = self.detailed_stats[session_id][-100:]

        self.last_update = time.time()

    def get_stats(self, session_id: str = None, user_id: int = None, group_id: int = None) -> dict:
        """获取Token统计信息"""
        if session_id:
            return {
                "session_tokens": self.session_tokens.get(session_id, 0),
                "session_calls": len(self.detailed_stats.get(session_id, [])),
                "last_call": self.detailed_stats.get(session_id, [{}])[-1].get('time', 0) if self.detailed_stats.get(
                    session_id) else 0
            }
        elif user_id:
            return {
                "user_tokens": self.user_tokens.get(str(user_id), 0)
            }
        elif group_id:
            return {
                "group_tokens": self.group_tokens.get(str(group_id), 0)
            }
        else:
            return {
                "total_tokens": self.total_tokens,
                "sessions": len(self.session_tokens),
                "users": len(self.user_tokens),
                "groups": len(self.group_tokens),
                "total_calls": sum(len(calls) for calls in self.detailed_stats.values())
            }

    def reset(self):
        """重置统计"""
        self.total_tokens = 0
        self.session_tokens.clear()
        self.user_tokens.clear()
        self.group_tokens.clear()
        self.detailed_stats.clear()
        self.last_update = time.time()


# 初始化全局Token统计
token_stats = TokenStats()


def add_token_usage(session_id: str, user_id: int = None, group_id: int = None,
                    tokens: int = 0, prompt_tokens: int = 0, completion_tokens: int = 0,
                    model: str = "deepseek-chat"):
    """添加真实Token使用记录"""
    token_stats.add_usage(session_id, user_id, group_id, tokens, prompt_tokens, completion_tokens, model)


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

        self._load_memory()

    def set_auto_compress(self, enabled: bool, threshold: int = None):
        self.auto_compress_enabled = bool(enabled)
        if threshold is not None:
            self.compress_after_messages = max(20, min(int(threshold), 80))

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
        """
        异步生成内容，自动保存记忆，并在需要时执行压缩
        """
        max_retries = len(key_manager.get_all_keys()) or 1
        last_error = None
        tried_keys = set()

        for attempt in range(max_retries):
            require_multimodal = isinstance(message, dict) and bool(message.get("image_urls"))
            current = key_manager.get_next_for_request(
                tried_keys=tried_keys,
                include_cooldown=True,
                require_multimodal=require_multimodal,
            )
            if not current:
                break

            base_url, current_key, model, supports_multimodal = current
            tried_keys.add(current_key)

            try:
                self._enforce_message_limit()
                image_urls = []
                if isinstance(message, dict):
                    user_content = str(message.get("text", "") or "")
                    image_urls = await prepare_image_inputs_for_model(
                        message.get("image_urls", []) or [],
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

                client = self._get_client(base_url, current_key)

                try:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.chat.completions.create,
                            model=model,
                            messages=messages,
                            stream=False,
                            timeout=API_REQUEST_TIMEOUT_SECONDS
                        ),
                        timeout=API_REQUEST_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    raise Exception(f"API 请求超过 {API_REQUEST_TIMEOUT_SECONDS} 秒未返回，已自动切换下一个")

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
                total_tokens = getattr(usage, "total_tokens", 0) if usage else 0
                prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
                completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

                self.history.append({
                    "role": "user",
                    "content": filter_sensitive_content(user_content)
                })
                self.history.append({
                    "role": "assistant",
                    "content": result
                })

                self._enforce_message_limit()
                key_manager.mark_success(current_key)

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

                return result, total_tokens, prompt_tokens, completion_tokens

            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}".lower()
                print(f"[DEBUG] API 调用失败 (key: {current_key[:8]}..., model: {model}): {e}")

                if "429" in error_msg or "rate limit" in error_msg or "rpm limit" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "503" in error_msg or "busy" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "500" in error_msg or "502" in error_msg or "504" in error_msg or "timeout" in error_msg or "403" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "invalid" in error_msg or "unauthorized" in error_msg or "401" in error_msg :
                    if key_manager.is_default_key(current_key):
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    else:
                        key_manager.disable_key(current_key, reason=str(e))
                    last_error = e
                    continue
                elif "model not exist" in error_msg or "not support" in error_msg or "404" in error_msg:
                    if key_manager.is_default_key(current_key):
                        key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    else:
                        key_manager.disable_key(current_key, reason=str(e))
                    last_error = e
                    continue
                elif "quota" in error_msg or "insufficient" in error_msg or "balance" in error_msg or "402" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "choices" in error_msg:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue
                elif "llm 回复命中切换关键词" in str(e).lower():
                    print(f"[LLM Failover] 回复命中关键词，切换下一个 API: model={model}, keyword={str(e)}")
                    key_manager.mark_failure(
                        current_key,
                        reason=str(e),
                        cooldown_seconds=get_api_failure_cooldown_seconds(),
                    )
                    last_error = e
                    continue
                else:
                    key_manager.mark_failure(current_key, reason=str(e), cooldown_seconds=get_api_failure_cooldown_seconds())
                    last_error = e
                    continue

        raise last_error or Exception("所有 API Key 均失败")


# ==================== 增强版ContextManager ====================
class EnhancedContextManager:
    """支持动态压缩和持久化的增强版上下文管理器"""

    def __init__(self):
        self.groups: dict[int, EnhancedLimitedDeepSeekContext] = {}
        self.private_chats: dict[int, EnhancedLimitedDeepSeekContext] = {}
        self.compressor = ContextCompressor(compression_threshold=int(user_cfg.get("compression_threshold", 40)))

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

        os.makedirs(os.path.join("data", 'compression'), exist_ok=True)
        stats_path = os.path.join("data", 'compression', 'compression_stats.json')

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

        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(serializable_stats, f, ensure_ascii=False, indent=2)

        return True

    except Exception as e:
        return False


def load_compression_stats(compressor=None):
    try:
        stats_path = os.path.join("data", 'compression', 'compression_stats.json')

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
            if os.path.exists("./temps/quote.png"):
                os.remove("./temps/quote.png")
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


# ==================== Token统计命令处理函数 ====================
async def handle_token_command(event, actions, is_group=True, order=""):
    """处理Token统计相关命令"""
    user_id = event.user_id

    if "token统计" in order or "查看token" in order or "token状态" in order:
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

            msg = f"📊 DeepSeek Token 消耗统计\n"
            msg += f"═══════════════\n"
            msg += f"💬 本次对话: {session_tokens} Token\n"
            msg += f"   ↳ 调用次数: {session_calls} 次\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"👥 本群总计: {group_stats['group_tokens']} Token\n"
            msg += f"🌐 全局总计: {global_stats['total_tokens']} Token\n"
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

            msg = f"📊 DeepSeek Token 消耗统计\n"
            msg += f"═══════════════\n"
            msg += f"💬 本次对话: {session_tokens} Token\n"
            msg += f"   ↳ 调用次数: {session_calls} 次\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"👤 你的总计: {user_stats['user_tokens']} Token\n"
            msg += f"🌐 全局总计: {global_stats['total_tokens']} Token\n"
            msg += f"   ↳ 总调用: {global_stats['total_calls']} 次\n"
            msg += f"━━━━━━━━━━━━━━\n"
            msg += f"📈 活跃会话: {global_stats['sessions']} 个\n"
            msg += f"👥 活跃群聊: {global_stats['groups']} 个"

            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(msg)))
        return True

    elif "重置token统计" in order and str(user_id) in ROOT_User:
        token_stats.reset()
        msg = "✅ 全局Token统计已重置"
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


# ==================== 压缩控制命令处理器 ====================
async def handle_compression_commands(event, actions, is_group=True, order=""):
    user_id = event.user_id
    has_permission = False

    if str(user_id) in ROOT_User or str(user_id) in Super_User or str(user_id) in Manage_User:
        has_permission = True

    if await handle_token_command(event, actions, is_group, order):
        return True

    if "压缩状态" in order or "压缩统计" in order:
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

    elif "立即压缩" in order or "手动压缩" in order:
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

    elif "查看时间线" in order and has_permission:
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

    elif "查看记忆列表" in order and has_permission:
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

    elif "自动压缩" in order:
        if not has_permission:
            msg = "❌ 你没有权限修改自动压缩设置"
            if is_group:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(msg)))
            else:
                await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(msg)))
            return True

        if "开启" in order:
            enabled = True
            action_msg = "开启"
        elif "关闭" in order:
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
        nums = re.findall(r'\d+', order)
        if nums:
            threshold = int(nums[0])
            if threshold < 20:
                threshold = 20
            if threshold > 80:
                threshold = 80

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

    elif "清除记忆" in order and has_permission:
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

    elif "全部压缩状态" in order and str(user_id) in ROOT_User:
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


def max_summarizable_msgs(group_id: str, max_tokens=800000) -> int:
    global chat_db
    history = chat_db[group_id]["history"]
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
            can_summary, message = can_summary_today(group_id)
            if not can_summary:
                return message

        db_to_use = temp_db if temp_db else chat_db

        total_tokens = sum(estimate_tokens(f"{msg['user']}: {msg['content']}")
                           for msg in list(db_to_use[group_id]["history"])[-n:])
        max_tokens = 800000

        if total_tokens > max_tokens:
            max_n = max_summarizable_msgs(group_id, max_tokens)
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


async def handle_node_messages(data: dict):
    temp_db = defaultdict(lambda: {
        "history": deque(maxlen=1000),
        "token_counter": 0
    })

    def add_to_temp_db(group_id: str, user: str, content: str):
        content = filter_sensitive_content(content)
        tokens = estimate_tokens(f"{user}: {content}")
        temp_db[group_id]["history"].append({"user": user, "content": content})
        temp_db[group_id]["token_counter"] += tokens
        return temp_db

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
                    add_to_temp_db("0", nickname, full_text)
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
                    add_to_temp_db("0", nickname, full_text)
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
                    add_to_temp_db("0", nickname, full_text)
                    message_count += 1

    return temp_db


# ==================== 天气查询功能 ====================
OPEN_METEO_WEATHER_CODE_MAP = {
    0: "晴朗", 1: "大部晴朗", 2: "局部多云", 3: "阴", 45: "有雾", 48: "冻雾",
    51: "小毛毛雨", 53: "毛毛雨", 55: "强毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒", 80: "阵雨", 81: "中等阵雨",
    82: "强阵雨", 85: "阵雪", 86: "强阵雪", 95: "雷暴", 96: "雷暴伴小冰雹", 99: "强雷暴伴冰雹",
}


def format_open_meteo_weather_data(weather_data: dict, city_name: str) -> str:
    try:
        display_city = weather_data.get("display_name") or city_name
        current = weather_data.get("current") or {}
        daily = weather_data.get("daily") or {}
        result = f"🌤️ {display_city} 天气预报\n" + "=" * 45 + "\n"

        current_temp = current.get("temperature_2m")
        current_humidity = current.get("relative_humidity_2m")
        current_code = current.get("weather_code")
        current_wind = current.get("wind_speed_10m")
        current_text = OPEN_METEO_WEATHER_CODE_MAP.get(current_code, f"天气代码 {current_code}") if current_code is not None else "天气未知"

        if any(v is not None for v in [current_temp, current_humidity, current_code, current_wind]):
            result += f"📍 实时: {current_text}"
            if current_temp is not None:
                result += f" | 🌡️ {current_temp}°C"
            result += "\n"
            if current_wind is not None:
                result += f"💨 风速: {current_wind} km/h\n"
            if current_humidity is not None:
                result += f"💧 湿度: {current_humidity}%\n"

        dates = daily.get("time") or []
        codes = daily.get("weather_code") or []
        temp_max_list = daily.get("temperature_2m_max") or []
        temp_min_list = daily.get("temperature_2m_min") or []
        shown_days = dates[:3]

        if shown_days:
            result += "─" * 45 + "\n"

        for i, fx_date in enumerate(shown_days):
            date_display = f"{fx_date[5:7]}/{fx_date[8:10]}" if isinstance(fx_date, str) and len(fx_date) >= 10 else str(fx_date)
            day_label = ["今天", "明天", "后天"][i] if i < 3 else date_display
            weather_code = codes[i] if i < len(codes) else None
            temp_max = temp_max_list[i] if i < len(temp_max_list) else None
            temp_min = temp_min_list[i] if i < len(temp_min_list) else None
            weather_text = OPEN_METEO_WEATHER_CODE_MAP.get(weather_code, f"天气代码 {weather_code}") if weather_code is not None else "天气未知"
            result += f"📅 {day_label} ({date_display})\n"
            if temp_min is not None and temp_max is not None:
                result += f"🌡️ 温度: {temp_min}°C ~ {temp_max}°C\n"
            result += f"☀️ 天气: {weather_text}\n"
            if i < len(shown_days) - 1:
                result += "─" * 45 + "\n"

        return filter_sensitive_content(result.rstrip())
    except Exception as e:
        return f"❌ 天气查询失败: {filter_sensitive_content(str(e))}"


async def get_weather_info(city_name: str) -> str:
    try:
        city_name = (city_name or "").strip()
        if not city_name:
            return "❌ 请输入要查询的城市名称"

        log_console("HTTP", f"天气 geo {city_name}")

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city_name, "count": 10, "language": "zh", "format": "json"},
                headers={"Accept": "application/json"},
            ) as response:
                if response.status != 200:
                    return f"❌ 城市查询失败 (HTTP {response.status})"
                geo_data = await response.json()

            results = geo_data.get("results") or []
            if not results:
                return f"❌ 未找到城市“{city_name}”，请尝试更具体的名称"

            location = pick_best_weather_location(results, city_name)
            if not location:
                return f"❌ 未找到城市“{city_name}”，请尝试更具体的名称"

            latitude = location.get("latitude")
            longitude = location.get("longitude")
            if latitude is None or longitude is None:
                return f"❌ 未找到城市坐标：{filter_sensitive_content(city_name)}"

            display_name = " · ".join([
                str(x) for x in [location.get("name"), location.get("admin1"), location.get("country")] if x
            ]) or city_name

            log_console("HTTP", f"天气 weather {display_name}")

            async with session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai",
                    "forecast_days": 3,
                },
                headers={"Accept": "application/json"},
            ) as response:
                if response.status != 200:
                    return f"❌ 天气服务暂时不可用 (HTTP {response.status})"
                weather_data = await response.json()

            weather_data["display_name"] = display_name
            return format_open_meteo_weather_data(weather_data, city_name)
    except Exception as e:
        return f"❌ 天气查询失败: {filter_sensitive_content(str(e))}"


# ==================== 图片生成功能 ====================



async def generate_image_with_apis(search_query, actions):
    search_query = filter_sensitive_content(search_query)
    log_console("HTTP", f"生图 search {_short_text(search_query, 40)}")

    search_apis = [
        {"url": "https://api.lolicon.app/setu/v2", "method": "GET", "key": "data",
         "array_key": True, "subkey": "urls.original", "type": "Pixiv插画", "search_param": "tag"},
        {"url": "https://api.yuanxiapi.cn/api/img", "method": "GET", "key": "imgurl",
         "type": "动漫", "params": {"type": "dongman"}},
        {"url": "https://api.yuanxiapi.cn/api/img", "method": "GET", "key": "imgurl",
         "type": "风景", "params": {"type": "fengjing"}},
    ]

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    search_mapping = {
        "动漫": "dongman", "二次元": "dongman", "动画": "dongman", "卡通": "dongman",
        "猫娘": "dongman", "兽耳": "dongman", "白毛": "dongman", "少女": "dongman",
        "萝莉": "dongman", "御姐": "dongman", "原神": "dongman", "东方": "dongman",
        "风景": "fengjing", "景色": "fengjing", "自然": "fengjing", "山水": "fengjing",
        "星空": "fengjing", "天空": "fengjing", "大海": "fengjing", "森林": "fengjing",
    }

    for api in search_apis:
        try:
            params = api.get('params', {}).copy()

            if search_query and search_query != "随机":
                if 'lolicon' in api['url']:
                    params['tag'] = search_query
                    params.update({'num': 1, 'r18': 0, 'excludeAI': 0})
                elif api.get('search_param'):
                    matched_type = None
                    for keyword, api_type in search_mapping.items():
                        if keyword in search_query:
                            matched_type = api_type
                            break
                    if matched_type and 'type' in params:
                        params['type'] = matched_type

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }

            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                if api['method'] == 'POST':
                    response = await session.post(api['url'], params=params, headers=headers, timeout=10)
                else:
                    response = await session.get(api['url'], params=params, headers=headers, timeout=10)

                if response.status == 200:
                    data = await response.json()

                    if api.get('array_key', False):
                        if data and 'data' in data and len(data['data']) > 0:
                            item = data['data'][0]
                            sensitive_tags = ["R-18", "R-18G", "r18", "成人", "NSFW"]
                            tags = item.get('tags', [])

                            has_sensitive = False
                            for tag in tags:
                                if any(s in str(tag).lower() for s in [t.lower() for t in sensitive_tags]):
                                    has_sensitive = True
                                    break

                            if has_sensitive:
                                continue

                            if api.get('subkey'):
                                keys = api['subkey'].split('.')
                                value = item
                                for key in keys:
                                    if value and key in value:
                                        value = value[key]
                                    else:
                                        value = None
                                        break
                                if value:
                                    info = f"Pixiv作品\n标题：{item.get('title', '未知')}\n作者：{item.get('author', '未知')}"
                                    return True, str(value), filter_sensitive_content(info)

                    elif api['key'] in data:
                        image_url = data[api['key']]
                        if image_url:
                            return True, str(image_url), f"来自 {api['type']} API"

        except Exception as e:
            continue

    return False, "", f"未找到与【{search_query}】相关的图片"


async def process_and_send(actions, event, ai_reply: str, is_group: bool, reply_to_first: bool = True):
    """
    处理AI回复，按分隔符拆分为多条消息并延迟发送
    支持带空格、大小写变体的 <split> 分隔符
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

    for idx, text in enumerate(parts):
        should_reply_current = idx == 0 and (should_reply_first or should_quote_split_first)

        if is_group:
            if should_reply_current:
                msg = Manager.Message(Segments.Reply(event_message_id), Segments.Text(text))
            else:
                msg = Manager.Message(Segments.Text(text))

            await actions.send(group_id=event.group_id, message=msg)
        else:
            await actions.send(user_id=event.user_id, message=Manager.Message(Segments.Text(text)))

        if idx < len(parts) - 1:
            delay = random.uniform(1.5, 3.5)
            await asyncio.sleep(delay)

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

        await process_and_send(actions, event, filter_sensitive_content(response), is_group=False)


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

        await process_and_send(actions, event, filter_sensitive_content(response), is_group=True, reply_to_first=False)

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
    ADMINS = Super_User + ROOT_User + Manage_User
    SUPERS = Super_User + ROOT_User

    try:
        event_user = (await actions.get_stranger_info(user_id)).data.raw
        event_user_nickname = filter_sensitive_content(event_user['nickname'])
    except:
        event_user_nickname = "用户"

    log_receive_private(user_id, event_user_nickname, event.message)

    # ==================== 插件基础上下文（私聊） ====================
    base_plugin_context = build_plugin_base_context(actions, event, ADMINS, SUPERS)

    # ==================== 执行 Any 插件 ====================
    plugin_context = base_plugin_context.copy()
    plugin_context.update({
        "event": event,
        "actions": actions,
        "user_id": user_id,
        "user_message": user_message,
        "order": "",
        "is_group": False,
    })
    if is_feature_enabled("plugins_external", False) and await execute_plugins(True, **plugin_context):
        return

    if user_message == "ping":
        await actions.send(user_id=user_id, message=Manager.Message(Segments.Text("pong! 私聊测试成功！v(◦'ωˉ◦)~♡")))
        return

    if is_feature_enabled("emoji_plus_one", True) and EMOJI_PLUS_ONE_ENABLED and has_emoji(user_message):
        if emoji_send_count is None or datetime.datetime.now() - emoji_send_count > datetime.timedelta(seconds=EMOJI_PLUS_ONE_COOLDOWN_SECONDS):
            await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(user_message)))
            emoji_send_count = datetime.datetime.now()
        return

    if user_message == "/reset" or user_message == "重置":
        await handle_reset_command(event, actions, is_group=False)
        return

    should_trigger = False
    order = ""
    is_image_generation = False

    if user_message.startswith(reminder):
        order_i = user_message.find(reminder)
        if order_i != -1:
            order = user_message[order_i + len(reminder):].strip()
            if order.startswith("生图"):
                is_image_generation = True
                should_trigger = True
            elif order:
                should_trigger = True

    # 处理压缩相关命令（私聊中也可用）
    if is_feature_enabled("compression_commands", True) and await handle_compression_commands(event, actions, is_group=False, order=order):
        return

    # ==================== 插件管理命令（私聊） ====================
    if is_feature_enabled("plugin_admin_commands", False) and user_message.startswith(reminder):
        if f"{reminder}重载插件" == user_message and str(user_id) in ADMINS:
            global plugins, loaded_plugins, disabled_plugins, failed_plugins, plugins_help
            plugins = load_plugins()
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(f"✅ 插件重载完成，当前 {len(loaded_plugins)} 个插件已加载")))
            return
        elif f"{reminder}禁用插件 " in user_message and str(user_id) in ADMINS:
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
        elif f"{reminder}启用插件 " in user_message and str(user_id) in ADMINS:
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
            lines.append(f"⭐ 默认设置: {key_manager.get_default_display()}")
            lines.append(f"🎯 当前使用: {key_manager.get_current_display()}")
            lines.append("")

            if not status_list:
                lines.append("暂无可用配置")
            else:
                for item in status_list:
                    flags = []
                    if item["is_current"]:
                        flags.append("当前")
                    if item.get("is_default"):
                        flags.append("默认")
                    flag_text = f" <- {'/'.join(flags)}" if flags else ""

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

        elif user_message.startswith(f"{reminder}modeldefault") and str(user_id) in ADMINS:
            arg = user_message[len(f"{reminder}modeldefault"):].strip()

            if not arg:
                content = f"⭐ {key_manager.get_default_display()}\n🎯 当前使用: {key_manager.get_current_display()}"
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text(content))
                )
                return

            if arg.lower() == "clear":
                key_manager.clear_default()
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text("✅ 已清除默认模型设置"))
                )
                return

            ok = False
            if arg.isdigit():
                ok = key_manager.set_default_by_index(int(arg))
            else:
                ok = key_manager.set_default_by_model(arg)

            if ok:
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text(f"✅ 默认模型设置成功\n{key_manager.get_default_display()}"))
                )
            else:
                await actions.send(
                    user_id=user_id,
                    message=Manager.Message(Segments.Text(f"❌ 默认模型设置失败：{arg}"))
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



    if is_feature_enabled("quote", True) and f"{reminder}名言" in user_message:
        await handle_quote_command(event, actions, is_group=False)
        return

    image_urls = extract_image_urls_from_message(event.message)
    has_images = bool(image_urls)
    if has_images and not is_image_generation:
        should_trigger = True

    if "帮助" in order or user_message == f"{reminder}帮助":
        content = f'''私聊模式 - 如何与{bot_name}交流( •̀ ω •́ )✧
——————————————
【基础对话】
1. 直接发送消息 —— 直接和我聊天
2. 发送图片 + 问题 —— {bot_name}会先识别图片内容，再结合你的问题回答
3. {reminder}帮助 —— 查看本帮助菜单
4. {reminder}关于 —— 查看{bot_name}的详细信息

【常用功能】
5. {reminder}天气 [城市] —— 查询天气，例如：{reminder}天气 北京
6. {reminder}生图 [搜索词] —— 按关键词找图/发图
7. {reminder}大头照 —— 获取你的头像大图
8. {reminder}名言 —— 将引用的消息生成名言图（需要先引用一条消息）

【记忆 / 上下文】
9. /reset 或 重置 —— 清除当前私聊对话记忆（任何人可用）
10. {reminder}注销 —— 清除当前私聊上下文并删除记忆（需要权限）
11. {reminder}压缩状态 —— 查看当前私聊的压缩状态
12. {reminder}立即压缩 —— 手动压缩当前私聊对话（需要权限）
13. {reminder}自动压缩 [开启/关闭] [阈值] —— 设置自动压缩（需要权限）
14. {reminder}查看时间线 —— 查看当前私聊时间线结构（需要权限）
15. {reminder}查看记忆列表 —— 查看所有已存储记忆（需要权限）
16. {reminder}清除记忆 —— 清除当前私聊记忆（需要权限）

【状态 / 统计】
17. {reminder}token统计 —— 查看 Token 消耗统计
18. {reminder}重置token统计 —— 重置全局 Token 统计（仅 ROOT 用户）
19. {reminder}感知 —— 查看运行状态（需要权限）
20. {reminder}开 [QQ号] —— 查询 QQ 资料；不填时默认查自己

【插件 / 模型管理】
21. {reminder}重载插件 —— 重新加载所有插件（需要权限）
22. {reminder}禁用插件 <插件名> —— 禁用指定插件（需要权限）
23. {reminder}启用插件 <插件名> —— 启用指定插件（需要权限）
24. {reminder}插件视角 —— 查看插件列表
25. {reminder}model —— 查看所有 API / 模型状态（需要权限）
26. {reminder}model <编号|模型名> —— 手动切换 API / 模型（需要权限）
27. {reminder}modeldefault —— 查看当前默认模型（需要权限）
28. {reminder}modeldefault <编号|模型名> —— 设置默认模型（需要权限）
29. {reminder}modeldefault clear —— 清除默认模型设置（需要权限）
30. {reminder}modellog —— 查看最近 API 切换日志（需要权限）
31. {reminder}启用model <编号> —— 手动恢复被禁用的 API（需要权限）
32. {reminder}重置model冷却 <编号> —— 清除某个 API 的冷却状态（需要权限）

【高级操作】
33. {reminder}重启 —— 保存记忆并重启机器人（需要权限）'''


        await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(content)))
        return

    elif "关于" in order:
        about = f'''{bot_name} {bot_name_en} - {project_name}
——————————————
Build Information
Version：{version_name}
Rebuilt from HypeR
'''
        await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(about)))
        return

    elif is_feature_enabled("weather", True) and "天气" in order:
        city_name = order.replace("天气", "").strip()
        if not city_name:
            await actions.send(user_id=user_id, message=Manager.Message(
                Segments.Text(f"请指定城市名称，例如：{reminder}天气 北京")))
            return

        await actions.send(user_id=user_id, message=Manager.Message(
            Segments.Text(f"{bot_name}正在查询 {city_name} 的天气... ☁️")))

        weather_result = await get_weather_info(city_name)
        await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(weather_result)))
        return

    elif "大头照" in order:
        await actions.send(user_id=user_id, message=Manager.Message(
            Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640")))
        return

    elif f"{reminder}注销" in user_message:
        if str(user_id) in Super_User or str(user_id) in ROOT_User or str(user_id) in Manage_User:
            cmc.clear_private_context(user_id)
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(f"私聊记忆已清除，{bot_name}重新开始~ (/≧▽≦)/")))
        else:
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(f"仅管理员可操作")))
        return

    elif f"{reminder}重启" in user_message:
        if str(user_id) in Super_User or str(user_id) in ROOT_User or str(user_id) in Manage_User:
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

    elif f"{reminder}感知" in user_message:
        if str(user_id) in Super_User or str(user_id) in ROOT_User or str(user_id) in Manage_User:
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
Token总计: {token_stats.total_tokens} Token（后台记录）
系统提示词: ✅ 独立存储'''
            for i, usage in enumerate(system_info["gpu_usage"]):
                feel = feel + f"\nGPU {i} 使用: {usage * 100:.2f}%"
            await actions.send(user_id=user_id, message=Manager.Message(Segments.Text(feel)))
        else:
            await actions.send(user_id=user_id,
                               message=Manager.Message(Segments.Text(f"仅管理员可操作")))
        return

    elif is_feature_enabled("image_generation", True) and "生图" in order and is_image_generation:
        search_query = order.replace("生图", "").strip()
        if not search_query:
            search_query = "随机"

        current_time = time.time()

        if user_id in cooldowns and current_time - cooldowns[user_id] < 18:
            if not (str(user_id) in Super_User or str(user_id) in ROOT_User or str(user_id) in Manage_User):
                time_remaining = 18 - (current_time - cooldowns[user_id])
                await actions.send(user_id=user_id, message=Manager.Message(
                    Segments.Text(f"18秒个人CD，请等待 {time_remaining:.1f} 秒后重试")))
                return

        sensitive_keywords = ["r18", "r-18", "成人", "nsfw", "エロ", "h", "性", "汁液", "胖次", "内裤", "内衣"]
        if any(keyword in search_query.lower() for keyword in sensitive_keywords):
            await actions.send(user_id=user_id, message=Manager.Message(
                Segments.Text(f"搜索词包含敏感内容，请更换其他搜索词 (╥﹏╥)")))
            return

        await actions.send(user_id=user_id, message=Manager.Message(
            Segments.Text(f"{bot_name}正在搜索图片【{search_query}】 ヾ(≧▽≦*)o")))

        try:
            success, image_url, image_info = await generate_image_with_apis(search_query, actions)

            if success and image_url:
                message_parts = []

                try:
                    image_segment = Segments.Image(image_url)
                    message_parts.append(image_segment)
                except Exception as img_error:
                    pass

                if image_info:
                    message_parts.append(Segments.Text(f"\n{filter_sensitive_content(image_info)}"))

                message_parts.append(Segments.Text(f"\n✨ 私聊图片生成完成！【{search_query}】"))

                await actions.send(user_id=user_id, message=Manager.Message(*message_parts))
                cooldowns[user_id] = current_time
            else:
                await actions.send(user_id=user_id, message=Manager.Message(
                    Segments.Text(f"未找到与【{search_query}】相关的图片，请尝试其他搜索词 (╥﹏╥)")))
        except Exception as e:
            await actions.send(user_id=user_id, message=Manager.Message(
                Segments.Text(build_user_error_text(e, error_type="program"))))

    elif should_trigger and order and not is_image_generation:
        # 先执行普通插件（非 Any）
        plugin_context = base_plugin_context.copy()
        plugin_context.update({
            "event": event,
            "actions": actions,
            "user_id": user_id,
            "user_message": user_message,
            "order": order,
            "is_group": False,
        })
        if is_feature_enabled("plugins_external", False) and await execute_plugins(False, **plugin_context):
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

            await process_and_send(actions, event, result, is_group=False)


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
            "user_message": user_message,
            "order": user_message.strip(),
            "is_group": False,
        })
        if is_feature_enabled("plugins_external", False) and await execute_plugins(False, **plugin_context):
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

            await process_and_send(actions, event, filter_sensitive_content(result), is_group=False)


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
    }


def load_plugins():
    global loaded_plugins, disabled_plugins, failed_plugins, plugins, plugins_help, reminder, bot_name
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
            if os.path.exists(setup_file):
                try:
                    unique_name = f"{filename}_{uuid.uuid4().hex}"
                    spec = importlib.util.spec_from_file_location(unique_name, setup_file)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[unique_name] = module
                    spec.loader.exec_module(module)

                    if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                        if isinstance(module.TRIGGHT_KEYWORD, str):
                            plugins.append(module)
                            loaded_plugins.append(unique_name)
                            if hasattr(module, 'HELP_MESSAGE') and isinstance(module.HELP_MESSAGE, str):
                                for line in module.HELP_MESSAGE.splitlines():
                                    if line.strip():
                                        plugins_help += f"\n       {line.strip()}"
                            print(f"✅ 已加载插件目录: {filename} (关键词: {module.TRIGGHT_KEYWORD})")
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
                spec.loader.exec_module(module)

                if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                    if isinstance(module.TRIGGHT_KEYWORD, str):
                        plugins.append(module)
                        loaded_plugins.append(unique_name)
                        if hasattr(module, 'HELP_MESSAGE') and isinstance(module.HELP_MESSAGE, str):
                            for line in module.HELP_MESSAGE.splitlines():
                                if line.strip():
                                    plugins_help += f"\n       {line.strip()}"
                        print(f"✅ 已加载插件: {module_name} (关键词: {module.TRIGGHT_KEYWORD})")
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
async def execute_plugins(isAny: bool, **main_context) -> bool:
    """执行插件，若任一插件返回 True 则中断后续处理"""
    user_message = main_context.get("order", "") if "order" in main_context else ""

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
                sig = inspect.signature(plugin_module.on_message)
                kwargs = {}
                for param_name, param in sig.parameters.items():
                    if param.kind in (
                        inspect.Parameter.VAR_POSITIONAL,
                        inspect.Parameter.VAR_KEYWORD,
                    ):
                        continue

                    if param_name in main_context:
                        kwargs[param_name] = main_context[param_name]
                    elif param.default is not inspect.Parameter.empty:
                        pass
                    else:
                        raise ValueError(f"插件 {plugin_module.__name__} 缺少参数 {param_name}")

                response = await plugin_module.on_message(**kwargs)
                if response is True:
                    return True
            except Exception as e:
                print(f"❌ 插件 {plugin_module.__name__} 执行出错: {e}")
                if not isAny:
                    return True
    return False


# ==================== 主事件处理器 ====================
@Listener.reg
@Logic.ErrorHandler().handle_async
async def handler(event: Events.Event, actions: Listener.Actions) -> None:
    global settings_loaded, bot_name, bot_name_en, reminder
    global chat_db, user_lists, second_start, EnableNetwork, generating
    global Super_User, Manage_User, ROOT_User, sys_prompt, emoji_send_count
    actions = LoggedActions(actions)

    if hasattr(event, 'user_id') and event.user_id == event.self_id:
        return

    all_blacklist = get_all_blacklist()

    if not settings_loaded:
        Read_Settings()
        settings_loaded = True

    # 管理员权限组（方便插件使用）
    ADMINS = Super_User + ROOT_User + Manage_User
    SUPERS = Super_User + ROOT_User

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
    if is_feature_enabled("plugins_external", False) and await execute_plugins(True, **plugin_context):
        return

    if isinstance(event, Events.GroupMemberIncreaseEvent):
        user = event.user_id
        group_id = event.group_id

        if is_user_blacklisted(str(group_id), all_blacklist):
            return

        try:
            user_info = await actions.get_stranger_info(user)
            user_nickname = filter_sensitive_content(user_info.data.raw.get('nickname', f"用户{user}"))

            welcome = f''' 加入{bot_name}的大家庭，{bot_name}是你最可爱的好朋友
经常@{bot_name} 看看{bot_name}又学会做什么新事情啦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''

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
                simple_welcome = f"欢迎 {user_nickname} 加入{bot_name}的大家庭！♪(≧∀≦)ゞ☆"
                await actions.send(
                    group_id=group_id,
                    message=Manager.Message(
                        Segments.At(user),
                        Segments.Text(f" {filter_sensitive_content(simple_welcome)}")
                    )
                )
        except Exception as e:
            pass
        return

    if isinstance(event, Events.NotifyEvent):
        if hasattr(event, 'notice_type') and event.notice_type == 'notify':
            if hasattr(event, 'sub_type') and event.sub_type == 'poke':
                if event.target_id == event.self_id:
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
        if is_user_blacklisted(event.user_id, all_blacklist):
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

        raw_order = user_message[len(reminder):].strip() if user_message.startswith(reminder) else ""
        if await handle_check_account_command(event, actions, raw_order, is_group=True):
            nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
            chat_db = add_message(event.group_id, nike, user_message)
            return

        if user_message == "/reset" or user_message == "重置":
            await handle_reset_command(event, actions, is_group=True)
            return

        if is_feature_enabled("quote", True) and f"{reminder}名言" in user_message:
            await handle_quote_command(event, actions, is_group=True)
            nike = await get_nickname_by_userid(event.user_id, Manager, actions, event.group_id)
            chat_db = add_message(event.group_id, nike, user_message)
            return

        if is_feature_enabled("summary", True) and user_message.startswith(reminder) and "总结" in user_message:
            nums = re.findall(r'\d+', user_message)
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
                            node_messages = await handle_node_messages(data)
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

        if is_feature_enabled("summary", True) and user_message.startswith(reminder) and ('聊天数据看板' in user_message or '数据看板' in user_message):
            if '@all' in user_message or '@全体' in user_message:
                if not (str(event.user_id) in ROOT_User or str(event.user_id) in Super_User or str(
                        event.user_id) in Manage_User):
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
                os.makedirs(os.path.join("data", 'sum_up'), exist_ok=True)
                pkl_path = os.path.join("data", 'sum_up', 'chat_db.pkl')

                serializable = {}
                for gid, data in chat_db.items():
                    serializable[str(gid)] = {
                        "history": list(data["history"]),
                        "token_counter": int(data["token_counter"])
                    }
                with open(pkl_path, 'wb') as f:
                    pickle.dump(serializable, f)
            except Exception as e:
                pass

        if "ping" == user_message:
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text("pong! 爆炸！v(◦'ωˉ◦)~♡ ")))
            return

        if is_feature_enabled("emoji_plus_one", True) and EMOJI_PLUS_ONE_ENABLED and has_emoji(user_message):
            if emoji_send_count is None or datetime.datetime.now() - emoji_send_count > datetime.timedelta(seconds=EMOJI_PLUS_ONE_COOLDOWN_SECONDS):
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(user_message)))
                emoji_send_count = datetime.datetime.now()
            return

        should_trigger = False
        order = ""
        is_image_generation = False
        is_wangkai_trigger = False
        is_at_trigger = False
        is_random_trigger = False

        if user_message.startswith(reminder):
            order_i = user_message.find(reminder)
            if order_i != -1:
                order = user_message[order_i + len(reminder):].strip()
                if order.startswith("生图"):
                    is_image_generation = True
                    should_trigger = True
                elif order:
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

        if is_feature_enabled("compression_commands", True) and await handle_compression_commands(event, actions, is_group=True, order=order):
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
            elif f"{reminder}禁用插件 " in user_message and str(event.user_id) in ADMINS:
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
            elif f"{reminder}启用插件 " in user_message and str(event.user_id) in ADMINS:
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
                lines.append(f"⭐ 默认设置: {key_manager.get_default_display()}")
                lines.append(f"🎯 当前使用: {key_manager.get_current_display()}")
                lines.append("")
                if not status_list:
                    lines.append("暂无可用配置")
                else:
                    for item in status_list:
                        flags = []
                        if item["is_current"]:
                            flags.append("当前")
                        if item.get("is_default"):
                            flags.append("默认")
                        flag_text = f" <- {'/'.join(flags)}" if flags else ""
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

            elif user_message.startswith(f"{reminder}modeldefault") and str(event.user_id) in ADMINS:
                arg = user_message[len(f"{reminder}modeldefault"):].strip()
                if not arg:
                    content = f"⭐ {key_manager.get_default_display()}\n🎯 当前使用: {key_manager.get_current_display()}"
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Text(content))
                    )
                    return
                if arg.lower() == "clear":
                    key_manager.clear_default()
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Text("✅ 已清除默认模型设置"))
                    )
                    return
                ok = False
                if arg.isdigit():
                    ok = key_manager.set_default_by_index(int(arg))
                else:
                    ok = key_manager.set_default_by_model(arg)
                if ok:
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(
                            Segments.Text(f"✅ 默认模型设置成功\n{key_manager.get_default_display()}"))
                    )
                else:
                    await actions.send(
                        group_id=event.group_id,
                        message=Manager.Message(Segments.Text(f"❌ 默认模型设置失败：{arg}"))
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
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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

        if f"{reminder}感知" in user_message:
            if str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User:
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
        Token总计: {token_stats.total_tokens} Token（后台记录）
        系统提示词: ✅ 独立存储'''
                for i, usage in enumerate(system_info["gpu_usage"]):
                    feel += f"\nGPU {i} 使用: {usage * 100:.2f}%"
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(feel)))
            else:
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Text("仅管理员可操作")))
            return

        if f"{reminder}管理员" in user_message:
            if str(event.user_id) in ROOT_User or str(event.user_id) in Super_User:
                content = f'''管理我们的{bot_name}
——————————————
你拥有管理{bot_name}的权限。若要查看普通帮助，请@{bot_name}
    1. {reminder}让我访问 —> 检索用有权限的用户
    2. {reminder}管理 M (QQ号，必填) —> 为用户添加 Manage_User 权限
    3. {reminder}管理 S (QQ号，必填) —> 为用户添加 Super_User 权限
    4. {reminder}删除管理 (QQ号，必填) —> 删除这个用户的全部权限
    5. {reminder}禁言 (@QQ+空格+时间(以秒为单位)，必填) —> 禁言用户一段时间
    6. {reminder}解禁 (@QQ，必填) —> 解除该用户禁言
    7. {reminder}踢出 (@QQ，必填) —> 将该用户踢出聊群
    8. 撤回 (引用一条消息) —> 撤回该消息
    9. {reminder}注销 —> 清除对话上下文并删除记忆
    10. {reminder}感知 —> 查看运行状态
    11. {reminder}核验 (QQ号，必填) —> 检索QQ账号信息
    12. {reminder}重启 —> 关闭所有线程和进程，关闭{bot_name}。然后重新启动{bot_name}。
    13. {reminder}添加黑名单 +空格 + 群号 —> 将该群加入群发黑名单
    14. {reminder}删除黑名单 +空格 + 群号 —> 将该群移除群发黑名单
    15. {reminder}列出黑名单 —> 列出黑名单中的所有群
    16. {reminder}总结以上N条消息 —> 总结指定数量的群聊消息（每日1次，最多{SUMMARY_MAX_MESSAGES}条）
    17. {reminder}聊天数据看板@全体 —> 查看所有群的统计数据
    18. {reminder}压缩状态 —> 查看本群对话压缩状态


你的每一步操作，与用户息息相关.'''
            elif str(event.user_id) in Manage_User:
                content = f'''管理我们的{bot_name}
——————————————
你拥有管理{bot_name}的权限。若要查看普通帮助，请@{bot_name}
    1. {reminder}让我访问 —> 检索用有权限的用户
    2. {reminder}注销 —> 清除对话上下文并删除记忆
    3. {reminder}感知 —> 查看运行状态
    4. {reminder}核验 (QQ号，必填) —> 检索QQ账号信息
    5. {reminder}重启 —> 关闭所有线程和进程，关闭{bot_name}。然后重新启动{bot_name}
    6. {reminder}禁言 (@QQ+空格+时间(以秒为单位)，必填) —> 禁言用户一段时间
    7. {reminder}解禁 (@QQ，必填) —> 解除该用户禁言
    8. {reminder}踢出 (@QQ，必填) —> 将该用户踢出聊群
    9. 撤回 (引用一条消息) —> 撤回该消息
    10. {reminder}添加黑名单 +空格 + 群号 —> 将该群加入群发黑名单
    11. {reminder}删除黑名单 +空格 + 群号 —> 将该群移除群发黑名单
    12. {reminder}列出黑名单 —> 列出黑名单中的所有群
    13. {reminder}总结以上N条消息 —> 总结指定数量的群聊消息（每日1次，最多{SUMMARY_MAX_MESSAGES}条）
    14. {reminder}聊天数据看板@全体 —> 查看所有群的统计数据
    15. {reminder}压缩状态 —> 查看本群对话压缩状态
    17. {reminder}立即压缩 —> 手动压缩本群对话（需要权限）
    18. {reminder}自动压缩 [开启/关闭] [阈值] —> 设置自动压缩（需要权限）
    19. {reminder}查看时间线 —> 查看本群对话时间线结构（需要权限）
    20. {reminder}查看记忆列表 —> 查看所有已存储的记忆（需要权限）
    21. {reminder}清除记忆 —> 清除本群对话记忆（需要权限）
    22. /reset 或 重置 —> 清除本群对话记忆（无需权限，任何人都可使用）
    23. {reminder}token统计 —> 查看Token消耗统计（仅手动查看）
    24. {reminder}重置token统计 —> 重置全局Token统计（仅ROOT用户）
    25. {reminder}重载插件 —> 重新加载所有插件（需要权限）
    26. {reminder}禁用插件 <插件名> —> 禁用指定插件（需要权限）
    27. {reminder}启用插件 <插件名> —> 启用指定插件（需要权限）
    28. {reminder}插件视角 —> 查看插件列表
    29. {reminder}model —> 查看所有 API / 模型状态（需要权限）
    30. {reminder}model <编号|模型名> —> 手动切换 API / 模型（需要权限）
    31. {reminder}modeldefault —> 查看当前默认模型（需要权限）
    32. {reminder}modeldefault <编号|模型名> —> 设置默认模型（需要权限）
    33. {reminder}modeldefault clear —> 清除默认模型设置（需要权限）
    34. {reminder}modellog —> 查看最近 API 切换日志（需要权限）
    35. {reminder}启用model <编号> —> 手动恢复被禁用的 API（需要权限）
    36. {reminder}重置model冷却 <编号> —> 清除某个 API 的冷却状态（需要权限）
你的每一步操作，与用户息息相关。'''

            else:
                content = "仅管理员可操作"

            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(content)))
            return

        if f"{reminder}帮助" in user_message:
            content = f'''如何与{bot_name}交流( •̀ ω •́ )✧
——————————————
【基础对话】
1. 艾特我 —— 直接触发对话
2. 句首加 {reminder} —— 用命令方式和我交流
3. 直接发送触发词 —— 若消息里带机器人触发词，也可能触发对话
4. {reminder}帮助 —— 查看本帮助菜单
5. {reminder}关于 —— 查看{bot_name}的详细信息

【常用功能】
6. {reminder}名言 —— 把引用消息生成名言图
7. {reminder}大头照 @某人 —— 获取对方头像大图；不 @ 则默认自己
8. {reminder}天气 [城市] —— 查询天气，例如：{reminder}天气 北京
9. {reminder}生图 [搜索词] —— 按关键词找图/发图
10. {reminder}总结以上N条消息 —— 总结指定数量的群聊消息
    限制：每个群每天最多 1 次，每次最多 {SUMMARY_MAX_MESSAGES} 条
11. {reminder}聊天数据看板 —— 查看当前群聊统计信息

【记忆 / 上下文】
12. /reset 或 重置 —— 清除当前群聊对话记忆（任何人可用）
13. {reminder}注销 —— 清除对话上下文并删除记忆（需要权限）
14. {reminder}压缩状态 —— 查看本群对话压缩状态
15. {reminder}立即压缩 —— 手动压缩本群对话（需要权限）
16. {reminder}自动压缩 [开启/关闭] [阈值] —— 设置自动压缩（需要权限）
17. {reminder}查看时间线 —— 查看本群对话时间线结构（需要权限）
18. {reminder}查看记忆列表 —— 查看所有已存储记忆（需要权限）
19. {reminder}清除记忆 —— 清除本群对话记忆（需要权限）

【状态 / 统计】
20. {reminder}token统计 —— 查看 Token 消耗统计
21. {reminder}重置token统计 —— 重置全局 Token 统计（仅 ROOT 用户）
22. {reminder}感知 —— 查看运行状态（需要权限）
23. {reminder}开 [QQ号] —— 查询 QQ 资料；不填时默认查自己

【插件 / 模型管理】
24. {reminder}重载插件 —— 重新加载所有插件（需要权限）
25. {reminder}禁用插件 <插件名> —— 禁用指定插件（需要权限）
26. {reminder}启用插件 <插件名> —— 启用指定插件（需要权限）
27. {reminder}插件视角 —— 查看插件列表
28. {reminder}model —— 查看所有 API / 模型状态（需要权限）
29. {reminder}model <编号|模型名> —— 手动切换 API / 模型（需要权限）
30. {reminder}modeldefault —— 查看或设置默认模型（需要权限）
31. {reminder}modellog —— 查看最近 API 切换日志（需要权限）
32. {reminder}启用model <编号> —— 手动恢复被禁用的 API（需要权限）
33. {reminder}重置model冷却 <编号> —— 清除某个 API 的冷却状态（需要权限）

【高级操作】
34. {reminder}重启 —— 保存记忆并重启机器人（需要权限）
35. {reminder}管理员 —— 查看管理员帮助（管理员可用）
'''
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(content)))
            return

        if "关于" in order:
            about = f'''{bot_name} {bot_name_en} - {project_name}
——————————————
Build Information
Version：{version_name}
Rebuilt from HypeR
'''
            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(about)))
            return

        if is_feature_enabled("weather", True) and "天气" in order:
            city_name = order.replace("天气", "").strip()
            if not city_name:
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Text(f"请指定城市名称，例如：{reminder}天气 北京")))
                return

            await actions.send(group_id=event.group_id,
                               message=Manager.Message(Segments.Text(f"{bot_name}正在查询 {city_name} 的天气... ☁️")))
            weather_result = await get_weather_info(city_name)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(weather_result)))
            return

        if "大头照" in order:
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

        if is_feature_enabled("image_generation", True) and "生图" in order and is_image_generation:
            search_query = order.replace("生图", "").strip()
            if not search_query:
                search_query = "随机"

            current_time = time.time()

            if event.user_id in cooldowns and current_time - cooldowns[event.user_id] < 18:
                if not (str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(
                        event.user_id) in Manage_User):
                    time_remaining = 18 - (current_time - cooldowns[event.user_id])
                    await actions.send(group_id=event.group_id,
                                       message=Manager.Message(
                                           Segments.Text(f"18秒个人CD，请等待 {time_remaining:.1f} 秒后重试")))
                    return

            sensitive_keywords = ["r18", "r-18", "成人", "nsfw", "エロ", "h", "性", "汁液", "胖次", "内裤", "内衣"]
            if any(keyword in search_query.lower() for keyword in sensitive_keywords):
                await actions.send(group_id=event.group_id,
                                   message=Manager.Message(Segments.Text(f"搜索词包含敏感内容，请更换其他搜索词 (╥﹏╥)")))
                return

            selfID = await actions.send(group_id=event.group_id,
                                        message=Manager.Message(
                                            Segments.Text(f"{bot_name}正在搜索图片【{search_query}】 ヾ(≧▽≦*)o")))

            try:
                success, image_url, image_info = await generate_image_with_apis(search_query, actions)

                if success and image_url:
                    message_parts = []

                    try:
                        image_segment = Segments.Image(image_url)
                        message_parts.append(image_segment)
                    except:
                        try:
                            image_segment = Segments.Image(file=image_url)
                            message_parts.append(image_segment)
                        except:
                            pass

                    if image_info:
                        info_lines = image_info.split('\n')
                        short_info = "\n".join(info_lines[:2])
                        message_parts.append(Segments.Text(f"\n{filter_sensitive_content(short_info)}"))

                    message_parts.append(Segments.Text(f"\n✨ 图片生成完成！【{search_query}】"))

                    try:
                        await actions.send(group_id=event.group_id, message=Manager.Message(*message_parts))
                        cooldowns[event.user_id] = current_time
                    except:
                        try:
                            await actions.send(group_id=event.group_id, message=Manager.Message(image_segment))
                            text_content = ""
                            if image_info:
                                info_lines = image_info.split('\n')
                                short_info = "\n".join(info_lines[:2])
                                text_content += f"\n{filter_sensitive_content(short_info)}"
                            text_content += f"\n✨ 图片生成完成！【{search_query}】"
                            if text_content.strip():
                                await actions.send(group_id=event.group_id,
                                                   message=Manager.Message(Segments.Text(text_content)))
                            cooldowns[event.user_id] = current_time
                        except:
                            pass
                else:
                    await actions.send(group_id=event.group_id,
                                       message=Manager.Message(
                                           Segments.Text(f"未找到与【{search_query}】相关的图片，请尝试其他搜索词 (╥﹏╥)")))

                try:
                    await actions.del_message(selfID.data.message_id)
                except:
                    pass

            except Exception as e:
                try:
                    await actions.del_message(selfID.data.message_id)
                except:
                    pass
            return

        if should_trigger and order and not is_image_generation:
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
            if is_feature_enabled("plugins_external", False) and await execute_plugins(False, **plugin_context):
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
                await process_and_send(actions, event, result, is_group=True, reply_to_first=reply_to_first)


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
    """运行机器人，带自动重试"""
    global running
    retry_count = 0
    max_retries = 5
    retry_delay = 5

    print(f"=== {bot_name} {bot_name_en} 启动中 ===")
    print(f"记忆存储: data/ai_memory/")
    print(f"动态压缩: 保留最近{user_cfg.get('compression_keep_recent', 20)}条消息，触发阈值{user_cfg.get('compression_threshold', 40)}条")
    print("=" * 20)
    set_connection_status("starting", "正在启动", "准备建立 OneBot / Hyper 连接")

    while running and retry_count < max_retries:
        try:
            print(f"尝试启动机器人... (第{retry_count + 1}次尝试)")
            set_connection_status("connecting", "连接中", f"第 {retry_count + 1} 次尝试连接 OneBot / Hyper")
            Listener.run()
            if HOT_SWITCH_IN_PROGRESS.is_set():
                HOT_SWITCH_IN_PROGRESS.clear()
                retry_count = 0
                print("♻️ 连接热切换已触发，立即按新配置重新建立连接...")
                continue
            if running:
                set_connection_status("disconnected", "已断开", "监听已退出，准备自动重连")

            if running:
                print("连接断开，准备重试...")

        except KeyboardInterrupt:
            print(f"\n{bot_name} 收到退出信号")
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

            retry_count += 1
            error_msg = str(e)
            set_connection_status("failed", "连接失败", error_msg)

            if "napcat" in error_msg.lower() or "连接" in error_msg or "连接失败" in error_msg:
                print(f"NapCat连接失败: {error_msg}")
            else:
                print(f"启动失败: {error_msg}")
                traceback.print_exc()

            if running and retry_count < max_retries:
                wait_time = retry_delay * retry_count
                print(f"等待 {wait_time} 秒后重试...")
                print("-" * 30)
                time.sleep(wait_time)
                continue
            else:
                print(f"达到最大重试次数 {max_retries}，退出")
                set_connection_status("stopped", "已停止", f"达到最大重试次数 {max_retries}")
                break

    print("机器人已停止运行")
    if not running:
        set_connection_status("stopped", "已停止", "机器人已停止运行")


def restart_current_process(reason: str = "配置变更"):
    """重启当前 Python 进程，使连接配置百分百按最新值生效。"""
    try:
        print(f"🔁 准备重启主进程：{reason}")
        set_connection_status("connecting", "重启中", reason)
    except Exception:
        pass

    try:
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
print(f"📊 全局Token统计: {token_stats.total_tokens} Token（仅后台记录）")
print(f"⚙️ 系统提示词独立存储: ✅ 已启用")
print("=" * 60)

# ==================== 加载插件 ====================
print("=" * 60)
print("🔌 正在检查外部插件加载状态...")
plugins = load_plugins() if is_feature_enabled("plugins_external", False) else []
if not is_feature_enabled("plugins_external", False):
    print("ℹ️ 外部插件加载已关闭，当前仅使用内置功能开关")
print(f"📦 插件帮助信息已收集: {len(plugins_help)} 字符")
print("=" * 60)

# 注册退出保存
atexit.register(lambda: save_compression_stats(cmc.compressor if 'cmc' in globals() else None))
atexit.register(lambda: print("🔄 正在保存所有AI记忆..."))

# ==================== 程序入口 ====================
if __name__ == "__main__":
    try:
        cleanup_legacy_config_files()
        Read_Settings()
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
        start_webui(on_config_saved=apply_runtime_config)
        run_with_retry()