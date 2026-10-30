# -*- coding: utf-8 -*-
"""按完整对话轮次处理带工具调用的消息历史。"""

from __future__ import annotations

import json
from typing import Any, Callable


def split_into_rounds(messages: list[dict]) -> list[list[dict]]:
    """一条 user 开始，到下一条 user 前的所有消息属于同一轮。"""
    rounds: list[list[dict]] = []
    current: list[dict] = []
    for message in messages:
        if message.get("role") == "user" and current:
            rounds.append(current)
            current = []
        current.append(message)
    if current:
        rounds.append(current)
    return rounds


def _tool_call_ids(message: dict) -> set[str]:
    return {
        str(call.get("id") or "")
        for call in message.get("tool_calls", [])
        if isinstance(call, dict) and call.get("id")
    }


def fix_messages(messages: list[dict]) -> list[dict]:
    """仅保留完整、ID 唯一的相邻工具组；不猜配对，也不修改原始消息。"""
    from copy import deepcopy

    # 同一 ID 跨 assistant 复用也有歧义，相关组全部失效，不为旧结果猜新调用。
    id_counts: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            call_id = call.get("id") if isinstance(call, dict) else None
            if isinstance(call_id, str) and call_id.strip():
                id_counts[call_id] = id_counts.get(call_id, 0) + 1

    fixed: list[dict] = []
    pending: dict | None = None
    expected: set[str] = set()
    pending_tools: dict[str, dict] = {}
    valid = False

    def flush() -> None:
        nonlocal pending, expected, pending_tools, valid
        if pending is not None:
            if valid and expected and expected == set(pending_tools):
                fixed.append(pending)
                fixed.extend(pending_tools.values())
            elif pending.get("content"):
                # 工具组无效只剥工具协议，保住助手普通正文；绝不伪造结果。
                pending.pop("tool_calls", None)
                fixed.append(pending)
        pending, expected, pending_tools, valid = None, set(), {}, False

    for original in messages:
        if not isinstance(original, dict):
            continue
        message = deepcopy(original)
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if (pending is not None and valid and isinstance(call_id, str)
                    and call_id in expected and call_id not in pending_tools):
                pending_tools[call_id] = message
            continue
        flush()
        if role == "assistant" and message.get("tool_calls"):
            pending = message
            calls = message["tool_calls"]
            valid = isinstance(calls, list) and bool(calls)
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict):
                    valid = False
                    continue
                call_id = call.get("id")
                function = call.get("function")
                if (not isinstance(call_id, str) or not call_id.strip()
                        or id_counts.get(call_id) != 1
                        or call.get("type") != "function"
                        or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str)
                        or not function["name"].strip()):
                    valid = False
                    continue
                expected.add(call_id)
                arguments = function.get("arguments")
                if isinstance(arguments, dict):
                    try:
                        function["arguments"] = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
                    except (TypeError, ValueError, RecursionError):
                        valid = False
                elif not isinstance(arguments, str):
                    valid = False
            continue
        if role in ("user", "assistant"):
            if role == "assistant":
                message.pop("tool_calls", None)
            fixed.append(message)
    flush()
    return fixed


SUMMARY_PREFIX = "[历史摘要，压缩了"


def is_summary_message(message: dict) -> bool:
    """判断是否为压缩生成的历史摘要消息。"""
    if message.get("role") != "assistant" or message.get("tool_calls"):
        return False
    content = message.get("content")
    return isinstance(content, str) and content.startswith(SUMMARY_PREFIX)


def keep_recent_rounds(messages: list[dict], max_rounds: int) -> list[dict]:
    """保留最近 max_rounds 个完整轮次，并始终保住开头的历史摘要。

    摘要是不带 user 的独立 assistant 消息，split_into_rounds 会把它算成单独一轮。
    若直接切尾部 N 轮，keep_recent >= max_rounds 时刚生成的摘要会立刻被丢掉，
    那次 LLM 压缩调用完全白费，早期上下文也丢得无声无息。
    """
    if max_rounds <= 0:
        return []
    rounds = split_into_rounds(messages)
    if not rounds:
        return []

    leading_summary: list[dict] = []
    if rounds and rounds[0] and is_summary_message(rounds[0][0]):
        first = rounds[0]
        # 摘要独占一轮时整轮摘出；若后面紧跟别的消息，只摘走摘要本身。
        if len(first) == 1:
            leading_summary = rounds.pop(0)
        else:
            leading_summary = [first[0]]
            rounds[0] = first[1:]

    budget = max_rounds - (1 if leading_summary else 0)
    if budget <= 0:
        # 预算连摘要都装不下时，优先保摘要——它代表被压掉的那一大段历史。
        return fix_messages(leading_summary)
    kept = [message for group in rounds[-budget:] for message in group]
    return leading_summary + fix_messages(kept)


def message_tokens(message: dict, estimator: "Callable[[str], int] | None" = None) -> int:
    """估算单条消息的 token 数，包含 tool_calls 的函数名与入参。

    只统计 content 会严重低估 agent 历史：assistant 发起工具调用时 content 往往是
    None，真正占 token 的是 tool_calls[].function.arguments——一条 8KB 的 shell 入参
    按 content 算是 0。预算守卫据此裁剪就等于没裁。
    """
    if estimator is None:
        from bot.estimate import estimate_tokens as estimator  # 延迟导入，避免循环依赖
    total = estimator(_text(message.get("content")))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict):
            continue
        total += estimator(str(function.get("name") or ""))
        total += estimator(str(function.get("arguments") or ""))
    return total


def messages_tokens(messages: "list[dict]", estimator: "Callable[[str], int] | None" = None) -> int:
    """估算一组消息的 token 总量。"""
    if estimator is None:
        from bot.estimate import estimate_tokens as estimator
    return sum(message_tokens(message, estimator) for message in messages)


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        return " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    if isinstance(content, (dict, tuple)):
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


def describe_message(message: dict, limit: int = 1000) -> str:
    role = str(message.get("role") or "unknown")
    if role == "assistant" and message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            calls.append(f"{function.get('name', '?')}({str(function.get('arguments', ''))[:200]})")
        text = "助手调用工具: " + ", ".join(calls)
    elif role == "tool":
        text = "工具结果: " + _text(message.get("content"))
    else:
        text = {"user": "用户", "assistant": "助手"}.get(role, role) + ": " + _text(message.get("content"))
    return text[:limit]


def extract_agent_trail(messages: list[dict], *,
                        request_user_messages: list[dict] | None = None) -> list[dict]:
    """提取工具链；仅请求级重试可按对象 identity 额外保留框架注入的 user。

    默认不提取 user，避免图片 data URL 进入普通持久化历史。旁表必须持有本轮
    实际注入的消息对象，不按内容、角色顺序或可伪造的消息字段猜测来源。
    """
    request_user_ids = {id(message) for message in (request_user_messages or [])}
    return [dict(message) for message in messages if (
        message.get("role") == "tool"
        or (message.get("role") == "assistant" and message.get("tool_calls"))
        or (message.get("role") == "user" and id(message) in request_user_ids)
    )]
