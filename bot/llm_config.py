# -*- coding: utf-8 -*-
"""Shared LLM provider/endpoint normalization helpers.

Runtime code path prefers llm_providers + llm_rotation.
llm_endpoints is retained only as a compatibility / derived cache.
"""
from __future__ import annotations

from typing import Any, Dict, List


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


def looks_like_placeholder_key(value: str) -> bool:
    """Check if value looks like a placeholder API key (using whitelist validation).

    Strategy: First use regex whitelist to validate common API key formats;
              if matched, treat as real key. Otherwise use blacklist detection.
    """
    import re

    text = str(value or "").strip()
    if not text:
        return True

    # Whitelist: common API key formats (OpenAI/Anthropic/etc)
    valid_key_patterns = [
        r'^sk-[A-Za-z0-9_-]{20,}$',           # OpenAI style
        r'^Bearer\s+[A-Za-z0-9_-]{20,}$',     # Bearer token
        r'^[A-Za-z0-9]{32,}$',                # Pure alphanumeric long string
        r'^[A-Za-z0-9-]{36}$',                # UUID format
        r'^[A-Za-z0-9_-]{40,}$',              # Long token
    ]

    for pattern in valid_key_patterns:
        if re.match(pattern, text):
            return False  # Matches real key format, not a placeholder

    # Blacklist: explicit placeholder patterns
    lowered = text.lower()
    exact_placeholders = {
        "your_api_key",
        "api_key",
        "sk-xxxx",
        "sk-***",
        "sk-xxx",
        "请输入api_key",
        "请输入api key",
        "your_key_here",
        "insert_key_here",
    }
    if lowered in exact_placeholders:
        return True

    # Substring blacklist
    suspicious_tokens = [
        "sk-xxxx",
        "sk-***",
        "在这里填写",
        "这里填",
        "示例",
        "例子",
        "占位",
        "测试key",
        "测试 key",
        "placeholder",
        "example",
        "sample",
    ]
    for token in suspicious_tokens:
        if token in lowered:
            return True

    # Special handling for Chinese input prompts
    if lowered.startswith("请输入") and len(text) < 30:
        return True
    if "请输入api" in lowered or "请输入 key" in lowered:
        return True

    return False


def normalize_provider_keys(value, *, strict_ascii: bool = False, log: bool = False) -> list[str]:
    if isinstance(value, str):
        raw_keys = [x.strip() for x in value.splitlines() if x.strip()]
    elif isinstance(value, list):
        raw_keys = [str(x).strip() for x in value if str(x).strip()]
    else:
        raw_keys = []

    result = []
    for raw_key in raw_keys:
        key = raw_key.strip().strip('"').strip("'").strip()
        if not key or looks_like_placeholder_key(key):
            continue
        if strict_ascii:
            try:
                key.encode("ascii")
            except UnicodeEncodeError:
                if log:
                    print(f"[API Key] 已忽略包含非 ASCII 字符的无效 Key: {key[:8]}...")
                continue
        result.append(key)
    return result


def provider_display_model(provider_id: str, model: str) -> str:
    provider_id = str(provider_id or "").strip()
    model = str(model or "").strip()
    return f"{provider_id}/{model}" if provider_id else model


# 思考等级：下拉可选项。空串 = 不发任何参数，跟随模型自己的默认行为。
# 之所以不给"默认=高"这种有主张的值：多数中转站按思考 token 计费，
# 替用户开高档等于悄悄多花钱。
REASONING_EFFORT_CHOICES = ("", "none", "minimal", "low", "medium", "high")


def normalize_reasoning_effort(value) -> str:
    """归一化思考等级。无法识别的值一律回落空串（不发参数）。"""
    text = str(value or "").strip().lower()
    # 兼容用户从别处抄来的大写写法与中文
    alias = {
        "off": "none", "close": "none", "disable": "none", "关闭": "none", "关": "none",
        "min": "minimal", "最小": "minimal",
        "低": "low", "中": "medium", "高": "high",
        "default": "", "auto": "", "默认": "",
    }
    text = alias.get(text, text)
    return text if text in REASONING_EFFORT_CHOICES else ""


def normalize_context_window(value) -> int:
    """归一化上下文窗口 token 数。0 表示未知 / 自动获取。"""
    try:
        window = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    # 负数没有意义；上限给一个宽松的防呆值，避免误填成字符数
    return max(0, min(window, 100_000_000))


def normalize_proxy_url(value) -> str:
    """把用户填的代理地址整理成 urllib / httpx / aiohttp 都能接受的形式。

    留空表示该提供商直连，不回退到全局代理（全局代理只管 GitHub 与 Agent）。
    只写 127.0.0.1:7890 这种缺协议的写法很常见，这里补成 http://。
    只接受 http/https：urllib 的 ProxyHandler 不支持 socks，httpx 也要额外装
    socksio 才行，放行 socks 只会让用户以为配好了而实际静默失败。
    """
    text = str(value or "").strip().strip('"').strip("'").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    scheme = text.split("://", 1)[0].lower()
    if scheme not in {"http", "https"}:
        return ""
    return text


def normalize_legacy_endpoints(value) -> list[dict]:
    """Normalize a legacy llm_endpoints list into runtime endpoint slots."""
    result = []
    if not isinstance(value, list):
        return result
    for raw in value:
        if not isinstance(raw, dict):
            continue
        base_url = str(raw.get("base_url", "") or "").strip()
        model = str(raw.get("model", "") or "").strip()
        keys = normalize_provider_keys(raw.get("keys", []), strict_ascii=True)
        if not base_url or not keys or not model:
            continue
        try:
            timeout_seconds = int(float(raw.get("timeout_seconds", 60) or 60))
        except (TypeError, ValueError):
            timeout_seconds = 60
        result.append({
            "provider_id": str(raw.get("provider_id", "") or "").strip(),
            "base_url": base_url,
            "model": model,
            "display_model": str(raw.get("display_model", "") or "").strip() or model,
            "keys": keys,
            "http_proxy": normalize_proxy_url(raw.get("http_proxy", "")),
            "supports_multimodal": normalize_bool_config(raw.get("supports_multimodal", False), False),
            "timeout_seconds": max(1, timeout_seconds),
            "reasoning_effort": normalize_reasoning_effort(raw.get("reasoning_effort", "")),
            "context_window": normalize_context_window(raw.get("context_window", 0)),
        })
    return result


def convert_legacy_endpoints_to_providers(others: Dict[str, Any]) -> list[dict]:
    """把旧 llm_endpoints 转成 providers。

    同一 base_url+keys 合并为一个 provider，多个 model 挂到 models 列表，
    避免每个 endpoint 都生成 providerN 导致 ID 膨胀。
    """
    converted = []
    grouped: dict[tuple[str, tuple[str, ...]], dict] = {}
    order: list[tuple[str, tuple[str, ...]]] = []
    endpoints = others.get("llm_endpoints", []) if isinstance(others.get("llm_endpoints", []), list) else []
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        model = str(ep.get("model", "") or "").strip()
        base_url = str(ep.get("base_url", "") or "").strip()
        keys = normalize_provider_keys(ep.get("keys", []))
        if not model or not base_url:
            continue
        key = (base_url, tuple(keys))
        if key not in grouped:
            provider_id = str(ep.get("provider_id", "") or "").strip() or f"provider{len(order) + 1}"
            # 若 id 冲突，追加序号
            existing_ids = {g.get("id") for g in grouped.values()}
            if provider_id in existing_ids:
                provider_id = f"{provider_id}_{len(order) + 1}"
            grouped[key] = {
                "id": provider_id,
                "base_url": base_url,
                "keys": keys,
                "http_proxy": normalize_proxy_url(ep.get("http_proxy", "")),
                "models": [],
                "detected_models": [],
            }
            order.append(key)
        try:
            timeout_seconds = int(float(ep.get("timeout_seconds", others.get("api_request_timeout_seconds", 60)) or 60))
        except (TypeError, ValueError):
            try:
                timeout_seconds = int(float(others.get("api_request_timeout_seconds", 60) or 60))
            except (TypeError, ValueError):
                timeout_seconds = 60
        names = {m.get("name") for m in grouped[key]["models"]}
        if model not in names:
            grouped[key]["models"].append({
                "name": model,
                "enabled": True,
                "supports_multimodal": normalize_bool_config(ep.get("supports_multimodal", False), False),
                "timeout_seconds": max(1, timeout_seconds),
                "reasoning_effort": normalize_reasoning_effort(ep.get("reasoning_effort", "")),
                "context_window": normalize_context_window(ep.get("context_window", 0)),
            })
    for key in order:
        converted.append(grouped[key])
    return converted


def normalize_llm_providers_config(others: Dict[str, Any]) -> tuple[list[Dict[str, Any]], list[Dict[str, str]]]:
    others = others if isinstance(others, dict) else {}
    providers = others.get("llm_providers", [])
    if not isinstance(providers, list):
        providers = []
    if not providers:
        providers = [{"id": "provider1", "base_url": "", "keys": [], "http_proxy": "", "models": [], "detected_models": []}]

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
        converted = convert_legacy_endpoints_to_providers(others)
        if converted:
            providers = converted

    normalized_providers = []
    seen_provider_ids = set()
    for raw in providers:
        if not isinstance(raw, dict):
            continue
        provider_id = str(raw.get("id", "") or "").strip()
        base_url = str(raw.get("base_url", "") or "").strip()
        keys = normalize_provider_keys(raw.get("keys", []))
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
            except (TypeError, ValueError):
                try:
                    timeout_seconds = int(float(others.get("api_request_timeout_seconds", 60) or 60))
                except (TypeError, ValueError):
                    timeout_seconds = 60
            if name in {m.get("name") for m in models}:
                continue
            models.append({
                "name": name,
                "enabled": normalize_bool_config(item.get("enabled", True), True),
                "supports_multimodal": normalize_bool_config(item.get("supports_multimodal", False), False),
                "timeout_seconds": max(1, timeout_seconds),
                # 思考等级：空串表示不发参数，跟随模型默认
                "reasoning_effort": normalize_reasoning_effort(item.get("reasoning_effort", "")),
                # 上下文窗口 token 数：0 表示未知，检测模型时会尝试自动填充
                "context_window": normalize_context_window(item.get("context_window", 0)),
            })
        raw_embedding_models = raw.get("embedding_models", []) if isinstance(raw.get("embedding_models", []), list) else []
        embedding_models = []
        for item in raw_embedding_models:
            if isinstance(item, str):
                item = {"name": item, "enabled": True}
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or item.get("model", "") or "").strip()
            if name and name not in {m.get("name") for m in embedding_models}:
                embedding_models.append({
                    "name": name,
                    "enabled": normalize_bool_config(item.get("enabled", True), True),
                    "dimensions": int(item.get("dimensions", 0) or 0),
                })
        detected = raw.get("detected_models", [])
        if isinstance(detected, str):
            detected = [x.strip() for x in detected.splitlines() if x.strip()]
        elif isinstance(detected, list):
            detected = [str(x).strip() for x in detected if str(x).strip()]
        else:
            detected = []
        # 重复 provider id：警告并使用第一个配置
        if provider_id and provider_id in seen_provider_ids:
            print(f"[警告] 检测到重复的渠道 ID: {provider_id}，将使用第一个配置并跳过后续重复项")
            continue
        if provider_id:
            seen_provider_ids.add(provider_id)
        normalized_providers.append({
            "id": provider_id,
            "base_url": base_url,
            "keys": keys,
            "http_proxy": normalize_proxy_url(raw.get("http_proxy", "")),
            "models": models,
            "embedding_models": embedding_models,
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
    provider_map = {}
    for provider in providers:
        provider_id = provider.get("id")
        if provider_id and provider_id not in provider_map:
            provider_map[provider_id] = provider
    result = []
    for item in rotation:
        provider = provider_map.get(item.get("provider_id"))
        if not provider:
            continue
        model_cfg = next(
            (
                m for m in provider.get("models", [])
                if m.get("name") == item.get("model") and m.get("enabled")
            ),
            None,
        )
        if not model_cfg:
            continue
        keys = normalize_provider_keys(provider.get("keys", []), strict_ascii=True)
        if not keys or not str(provider.get("base_url", "") or "").strip():
            continue
        result.append({
            "provider_id": provider.get("id", ""),
            "base_url": provider.get("base_url", ""),
            "model": model_cfg.get("name", ""),
            "display_model": provider_display_model(provider.get("id", ""), model_cfg.get("name", "")),
            "keys": keys,
            "http_proxy": normalize_proxy_url(provider.get("http_proxy", "")),
            "supports_multimodal": bool(model_cfg.get("supports_multimodal", False)),
            "timeout_seconds": int(model_cfg.get("timeout_seconds", others.get("api_request_timeout_seconds", 60)) or 60),
            "reasoning_effort": normalize_reasoning_effort(model_cfg.get("reasoning_effort", "")),
            "context_window": normalize_context_window(model_cfg.get("context_window", 0)),
        })
    return result


def build_all_provider_endpoints(others: Dict[str, Any]) -> list[Dict[str, Any]]:
    """列出所有可调用的 provider/model 组合，不管模型有没有勾选。

    models[].enabled 的语义是「是否参与 QQ 侧的自动轮换」，不代表该模型不可用。
    WebUI 聊天室是手动选模型，所以要能看到并选中未勾选的模型。
    顺序：先按轮换列表（已勾选的，保持轮换顺序），再补未勾选的，
    这样聊天室的失败切换仍优先走用户配置的轮换顺序。
    """
    providers, _ = normalize_llm_providers_config(others)
    rotation_refs = [
        (ep.get("provider_id", ""), ep.get("model", ""))
        for ep in build_llm_endpoints_from_providers(others)
    ]
    default_timeout = others.get("api_request_timeout_seconds", 60)
    by_ref: Dict[tuple, Dict[str, Any]] = {}
    order: list[tuple] = []
    for provider in providers:
        provider_id = str(provider.get("id", "") or "")
        base_url = str(provider.get("base_url", "") or "").strip()
        keys = normalize_provider_keys(provider.get("keys", []), strict_ascii=True)
        if not base_url or not keys:
            continue
        proxy = normalize_proxy_url(provider.get("http_proxy", ""))
        for model_cfg in provider.get("models", []):
            if not isinstance(model_cfg, dict):
                continue
            name = str(model_cfg.get("name", "") or "").strip()
            if not name:
                continue
            ref = (provider_id, name)
            if ref in by_ref:
                continue
            by_ref[ref] = {
                "provider_id": provider_id,
                "base_url": base_url,
                "model": name,
                "display_model": provider_display_model(provider_id, name),
                "keys": keys,
                "http_proxy": proxy,
                "supports_multimodal": bool(model_cfg.get("supports_multimodal", False)),
                "timeout_seconds": int(model_cfg.get("timeout_seconds", default_timeout) or 60),
                "enabled": normalize_bool_config(model_cfg.get("enabled", True), True),
                "reasoning_effort": normalize_reasoning_effort(model_cfg.get("reasoning_effort", "")),
                "context_window": normalize_context_window(model_cfg.get("context_window", 0)),
            }
            order.append(ref)
    result = []
    seen = set()
    for ref in rotation_refs:
        if ref in by_ref and ref not in seen:
            seen.add(ref)
            result.append(by_ref[ref])
    for ref in order:
        if ref not in seen:
            seen.add(ref)
            result.append(by_ref[ref])
    return result


def get_provider_proxy(others: Dict[str, Any], provider_id: str) -> str:
    """按 provider id 取该提供商配置的代理；没配就返回空串（直连）。"""
    if not isinstance(others, dict):
        return ""
    providers = others.get("llm_providers", [])
    if not isinstance(providers, list):
        return ""
    target = str(provider_id or "").strip()
    if not target:
        return ""
    for provider in providers:
        if isinstance(provider, dict) and str(provider.get("id", "") or "").strip() == target:
            return normalize_proxy_url(provider.get("http_proxy", ""))
    return ""


def get_provider_proxy_for_base_url(others: Dict[str, Any], base_url: str) -> str:
    """按 base_url 反查提供商代理。

    保留给只知道 base_url、拿不到 provider_id 的旧调用方（例如外部插件）。
    运行时主路径已改为由 key_manager 随轮换结果直接给出 http_proxy，
    不再走这里；多个提供商共用同一 base_url 时这个函数无法区分。
    """
    if not isinstance(others, dict):
        return ""
    providers = others.get("llm_providers", [])
    if not isinstance(providers, list):
        return ""
    target = str(base_url or "").strip().rstrip("/")
    if not target:
        return ""
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if str(provider.get("base_url", "") or "").strip().rstrip("/") != target:
            continue
        proxy = normalize_proxy_url(provider.get("http_proxy", ""))
        if proxy:
            return proxy
    return ""


def sync_provider_config(others: Dict[str, Any]) -> None:
    providers, rotation = normalize_llm_providers_config(others)
    others["llm_providers"] = providers
    others["llm_rotation"] = rotation
    # Keep a derived cache for old readers; providers remain the source of truth.
    others["llm_endpoints"] = build_llm_endpoints_from_providers(others)


def force_apply_llm_endpoints_from_config(cfg: Dict[str, Any], set_endpoints=None) -> list[Dict[str, Any]]:
    """Build runtime endpoint slots and optionally push into key_manager."""
    others = cfg.get("Others", {}) if isinstance(cfg, dict) else {}
    if not isinstance(others, dict):
        others = cfg if isinstance(cfg, dict) else {}
    endpoints = build_llm_endpoints_from_providers(others)
    if not endpoints:
        endpoints = normalize_legacy_endpoints(others.get("llm_endpoints", []))
    if callable(set_endpoints):
        set_endpoints(endpoints)
    return endpoints


def normalize_llm_provider_rotation(cfg: dict) -> list:
    """Runtime entry used by main.py / key_manager."""
    cfg = cfg or {}
    # main.py 传入的是 load_user_cfg() 的扁平 Others 字典
    endpoints = build_llm_endpoints_from_providers(cfg)
    if endpoints:
        return endpoints
    # Compatibility fallback for configs that still only have llm_endpoints.
    return normalize_legacy_endpoints(cfg.get("llm_endpoints", []))
