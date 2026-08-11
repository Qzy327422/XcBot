# -*- coding: utf-8 -*-
"""Agent 对话轮次统一上下文。

参考 AstrBot 的 run_context.messages 设计，
把当前 agent 对话轮次的所有消息（用户消息、工具调用、工具结果、最终回复）
集中管理，成功或降级后一次性提交到会话历史。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTurnContext:
    """当前对话轮次的统一上下文。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    # seed() 之后的消息条数。工具循环只会往列表尾部追加，所以
    # messages[baseline:] 就是本轮新产生的工具链，不含被载入的历史。
    baseline: int = 0

    def seed(self, messages: list[dict[str, Any]]) -> None:
        """载入本轮请求的初始消息（系统提示词 + 历史 + 当前用户消息）。

        必须传副本给工具循环：它会就地追加工具中间消息，直接把 _build_messages
        的结果交出去会污染调用方的列表。同时记下边界，提交历史时只取新增部分——
        历史里已经存了往轮的工具链，全量提取会让它每轮翻倍。
        """
        self.messages = list(messages)
        self.baseline = len(self.messages)

    def new_messages(self) -> list[dict[str, Any]]:
        """本轮工具循环新追加的消息（不含 seed 进来的历史）。"""
        return self.messages[self.baseline:]

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        self.messages.extend(messages)

    def clear(self) -> None:
        self.messages.clear()
        self.baseline = 0

    def snapshot(self) -> list[dict[str, Any]]:
        """返回当前消息列表的浅拷贝，供后续处理使用。"""
        return list(self.messages)

    def is_empty(self) -> bool:
        return len(self.messages) == 0
