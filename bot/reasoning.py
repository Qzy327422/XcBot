# -*- coding: utf-8 -*-
"""思考模型（推理模型）的思维链处理。

两种常见形态，这里都要认：

1. 独立字段：``choices[0].message.reasoning_content``（DeepSeek、MiMo、多数中转站）
   或 ``reasoning``（OpenRouter 风格）。SDK 把它当额外字段挂在对象上。
2. 内嵌标签：思维链混在 ``content`` 里，用 ``<think>...</think>`` 包裹
   （常见于自建 R1 蒸馏、部分中转站的转换层）。

处理原则：思维链**不进正文、不进历史、不发给用户**，只在追踪页与
WebUI 聊天室的可折叠区域里展示。原因有三个：

- 思维链里经常出现"作为AI""角色扮演"这类词，会命中回复切换关键词，
  导致明明回复正常却一路换模型直到全部失败。
- 思维链动辄几千字，进历史会迅速吃掉上下文预算。
- QQ 侧分段发送会把思维链当正文切成十几条消息发出去。
"""
from __future__ import annotations

import re

# 兼容 <think>、<thinking>、<thought>；DOTALL 让 . 匹配换行。
_THINK_BLOCK = re.compile(r"<(think|thinking|thought)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
# 只有开标签没有闭标签：模型被截断时常见，后面全都是思维链。
_THINK_OPEN_ONLY = re.compile(r"<(think|thinking|thought)>(.*)$", re.IGNORECASE | re.DOTALL)
# 只有闭标签没有开标签：部分模型不吐开标签，闭标签之前的都是思维链。
_THINK_CLOSE_ONLY = re.compile(r"^(.*?)</(think|thinking|thought)>", re.IGNORECASE | re.DOTALL)

# 独立字段的候选名，按优先级。
REASONING_FIELDS = ("reasoning_content", "reasoning")


def strip_think_tags(text: str) -> tuple[str, str]:
    """剥离内嵌的 <think> 块，返回 (正文, 思维链)。

    三种残缺形态都处理：成对、只有开标签、只有闭标签。
    """
    raw = str(text or "")
    if not raw:
        return "", ""
    if "<" not in raw:
        return raw, ""

    reasoning_parts: list[str] = []

    def _take(match: re.Match) -> str:
        reasoning_parts.append(match.group(2))
        return ""

    visible = _THINK_BLOCK.sub(_take, raw)

    # 成对的处理完之后，再看有没有残缺的单边标签。
    # 只有开标签时不能无条件吞掉后面全部内容：模型正文里正常出现 "<think>"
    # 字样（讨论 prompt 写法时很常见）会把整段回复吞成思维链。这里要求开标签
    # 出现在靠前位置（前 40 字符内）才认——真正被截断的思考总是从头开始。
    if re.search(r"<(think|thinking|thought)>", visible, re.IGNORECASE):
        m = _THINK_OPEN_ONLY.search(visible)
        if m and m.start() <= 40:
            reasoning_parts.append(m.group(2))
            visible = visible[: m.start()]
    elif re.search(r"</(think|thinking|thought)>", visible, re.IGNORECASE):
        m = _THINK_CLOSE_ONLY.search(visible)
        if m:
            reasoning_parts.append(m.group(1))
            visible = visible[m.end():]

    reasoning = "\n\n".join(p.strip() for p in reasoning_parts if p and p.strip())
    return visible.strip(), reasoning.strip()


def extract_field_reasoning(message, strip: bool = True) -> str:
    """从 SDK 的 message / delta 对象上取独立字段形式的思维链。

    中转站常把该字段设为空字符串占位，所以取到空串也算"有这个字段但没内容"，
    直接返回空串即可，不需要区分。

    strip 只在非流式（一次拿到完整思维链）时为 True。流式必须传 False：
    思维链是逐 token 来的（" The" / " user" / " wants"），对每个分片 strip
    会把词间空格全吃掉，页面上就变成 "Theuserwants" 一坨。
    """
    if message is None:
        return ""
    for name in REASONING_FIELDS:
        value = getattr(message, name, None)
        if value is None and isinstance(message, dict):
            value = message.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip() if strip else value
    return ""


def split_model_output(message, raw_content: str = None) -> tuple[str, str]:
    """统一入口：返回 (给用户看的正文, 思维链)。

    字段优先于标签 —— 字段是厂商正式接口，标签是兜底。两者都有时合并，
    因为存在"字段给摘要、正文里还留标签"的中转站。
    """
    content = raw_content if raw_content is not None else str(getattr(message, "content", "") or "")
    visible, tag_reasoning = strip_think_tags(content)
    field_reasoning = extract_field_reasoning(message)
    parts = [p for p in (field_reasoning, tag_reasoning) if p]
    # 去重：有的中转站既给 reasoning_content 字段、又在正文里留一份同样的
    # <think> 内容，两份合并会让页面把同一段思考显示两遍。
    if len(parts) == 2 and (parts[0] in parts[1] or parts[1] in parts[0]):
        parts = [max(parts, key=len)]
    return visible, "\n\n".join(parts)


_ANY_OPEN = re.compile(r"<(think|thinking|thought)>", re.IGNORECASE)
_ANY_CLOSE = re.compile(r"</(think|thinking|thought)>", re.IGNORECASE)
_MAX_TAG_LEN = len("</thinking>")


class ThinkStreamSplitter:
    """流式增量里的 <think> 剥离器。

    流式下标签会被切碎（``<thi`` + ``nk>``），不能对单个 chunk 做正则。
    这里维护一个小缓冲：只要尾部可能是半截标签就先扣住不发，等下一个
    chunk 拼上再判断。``flush()`` 在流结束时把扣住的内容放出来。
    """

    def __init__(self):
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> tuple[str, str]:
        """喂入一个增量，返回本次可以发出的 (正文, 思维链)。"""
        self._buf += str(chunk or "")
        visible_out: list[str] = []
        reasoning_out: list[str] = []
        while True:
            if self._in_think:
                m = _ANY_CLOSE.search(self._buf)
                if not m:
                    # 没见到闭标签，缓冲里除了可能的半截闭标签都是思维链
                    safe = self._safe_len(self._buf)
                    if safe:
                        reasoning_out.append(self._buf[:safe])
                        self._buf = self._buf[safe:]
                    break
                reasoning_out.append(self._buf[: m.start()])
                self._buf = self._buf[m.end():]
                self._in_think = False
                continue
            m = _ANY_OPEN.search(self._buf)
            if not m:
                safe = self._safe_len(self._buf)
                if safe:
                    visible_out.append(self._buf[:safe])
                    self._buf = self._buf[safe:]
                break
            visible_out.append(self._buf[: m.start()])
            self._buf = self._buf[m.end():]
            self._in_think = True
        return "".join(visible_out), "".join(reasoning_out)

    @staticmethod
    def _safe_len(buf: str) -> int:
        """返回可以安全放行的长度：尾部可能是半截标签的部分要扣住。"""
        if not buf:
            return 0
        tail_start = max(0, len(buf) - _MAX_TAG_LEN)
        idx = buf.rfind("<", tail_start)
        return len(buf) if idx < 0 else idx

    def flush(self) -> tuple[str, str]:
        """流结束：把缓冲里剩下的全部放出。

        仍在 think 里说明模型被截断且没吐闭标签，剩下的算思维链。
        """
        rest, self._buf = self._buf, ""
        if not rest:
            return "", ""
        return ("", rest) if self._in_think else (rest, "")

