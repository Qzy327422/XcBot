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
    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    # 精确匹配常见占位符；不要用 "api_key" 子串，否则会误杀 sk-...api_key... 之类真 key
    exact = {
        "your_api_key",
        "api_key",
        "sk-xxxx",
        "sk-***",
        "sk-xxx",
        "请输入api_key",
        "请输入api key",
    }
    if lowered in exact:
        return True
    tokens = [
        "your_api_key",
        "sk-xxxx",
        "sk-***",
        "在这里填写",
        "这里填",
        "示例",
        "例子",
        "占位",
        "测试key",
        "测试 key",
        "请输入",
    ]
    # “请输入”只在整段像占位提示时才拦截，避免误伤真实 key 文本
    for token in tokens:
        if token == "请输入":
            if lowered.startswith("请输入") or "请输入api" in lowered or "请输入 key" in lowered:
                return True
            continue
        if token in lowered:
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
            "supports_multimodal": normalize_bool_config(raw.get("supports_multimodal", False), False),
            "timeout_seconds": max(1, timeout_seconds),
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
            })
        detected = raw.get("detected_models", [])
        if isinstance(detected, str):
            detected = [x.strip() for x in detected.splitlines() if x.strip()]
        elif isinstance(detected, list):
            detected = [str(x).strip() for x in detected if str(x).strip()]
        else:
            detected = []
        # 重复 provider id 会导致后写覆盖前写；运行时只保留首个，避免 silent overwrite
        if provider_id and provider_id in seen_provider_ids:
            print(f"[模型配置] 已忽略重复渠道 ID：{provider_id}")
            continue
        if provider_id:
            seen_provider_ids.add(provider_id)
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
            "supports_multimodal": bool(model_cfg.get("supports_multimodal", False)),
            "timeout_seconds": int(model_cfg.get("timeout_seconds", others.get("api_request_timeout_seconds", 60)) or 60),
        })
    return result


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
