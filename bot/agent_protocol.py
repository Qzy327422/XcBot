"""纯协议边界：绝不从正文提取或执行工具调用。

调用方必须先拒绝 ``NormalizedToolCall.error`` 非空的调用，再把
``raw_arguments``（而非 history 字符串）交给参数解析器。流式调用方可用
``markup_prefix_pending`` 暂存未完成的标记头，最终对累计 assistant 正文调用
``inspect_tool_markup``。两个正文 helper 均不解释函数名、参数值或 command。
"""
from __future__ import annotations

import copy
import json
import re
import unicodedata
import uuid
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "NormalizedFunctionCall", "NormalizedToolCall", "normalize_tool_calls",
    "inspect_tool_markup", "markup_prefix_pending",
]

_INVALID_NAME = "__invalid_tool_call__"
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MISSING = object()


@dataclass
class NormalizedFunctionCall:
    name: str
    arguments: str = field(repr=False)


@dataclass
class NormalizedToolCall:
    id: str
    function: NormalizedFunctionCall
    type: str = "function"
    error: str = ""
    raw_arguments: Any = field(default=None, repr=False)


def _field(obj: Any, name: str, default: Any = _MISSING) -> Any:
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        # 属性读取异常的 SDK 形状对象同样必须安全拒绝。
        return default


def _call_id(raw_id: Any, used_ids: set[str]) -> str:
    # ID 不使用函数名正则：保留服务商标点，但拒绝空白和 Unicode 控制字符。
    valid = (isinstance(raw_id, str) and 1 <= len(raw_id) <= 64
             and not any(char.isspace() or unicodedata.category(char).startswith("C")
                         for char in raw_id))
    if not valid or raw_id in used_ids:
        raw_id = "call_" + uuid.uuid4().hex
        while raw_id in used_ids:
            raw_id = "call_" + uuid.uuid4().hex
    used_ids.add(raw_id)
    return raw_id


def _reject_constant(value: str) -> None:
    raise ValueError("JSON 数值不能是 NaN 或 Infinity")


def _arguments(raw: Any) -> tuple[str, Any, str]:
    if raw is None:
        return "{}", None, ""
    if isinstance(raw, str):
        # 坏 JSON 字符串原样留给调用方解析或修复；合法标量/数组不是参数对象。
        try:
            parsed = json.loads(raw, parse_constant=_reject_constant)
        except json.JSONDecodeError:
            return raw, raw, ""
        except (ValueError, RecursionError):
            return raw, raw, "参数包含不合法的 JSON 数值或层级"
        if not isinstance(parsed, dict):
            return raw, raw, "参数必须是 JSON 对象"
        return raw, raw, ""
    if isinstance(raw, dict):
        try:
            detached = copy.deepcopy(raw)
            history = json.dumps(detached, ensure_ascii=False, allow_nan=False)
        except Exception:
            # 异常文本可能带参数值，因此只能返回固定诊断。
            return "{}", raw, "参数对象无法序列化为合法 JSON"
        return history, detached, ""
    return "{}", raw, "参数必须是字符串、字典对象或 None"


def normalize_tool_calls(
    raw_calls: Any, used_ids: set[str] | None = None,
) -> list[NormalizedToolCall]:
    """仅规范化标准 SDK/dict 调用，不修改输入对象。

    所有返回 ID（包括错误调用）都会加入 ``used_ids``。坏 JSON 字符串交给
    参数解析器；不支持的对象及非对象根 JSON 均报错。诊断长度有界，绝不
    复制不可信的名称、ID、参数值或异常文本。
    """
    ids = used_ids if used_ids is not None else set()
    if not isinstance(raw_calls, (list, tuple)):
        return [NormalizedToolCall(
            id=_call_id(None, ids),
            function=NormalizedFunctionCall(_INVALID_NAME, "{}"),
            error="tool_calls 必须是 list 或 tuple",
        )]

    result = []
    for call in raw_calls:
        errors = []
        invalid_function = False
        if _field(call, "type") != "function":
            errors.append("工具调用 type 必须是 function")
            invalid_function = True
        function = _field(call, "function")
        if function is _MISSING or function is None:
            errors.append("工具调用缺少 function 对象")
            invalid_function = True
        name = _field(function, "name")
        name = name.strip() if isinstance(name, str) else ""
        if not _NAME.fullmatch(name):
            errors.append("函数名必须匹配 [A-Za-z0-9_-]{1,64}")
            invalid_function = True
        raw = _field(function, "arguments", None)
        arguments, detached, argument_error = _arguments(raw)
        if argument_error:
            errors.append(argument_error)
        result.append(NormalizedToolCall(
            id=_call_id(_field(call, "id", None), ids),
            function=NormalizedFunctionCall(
                _INVALID_NAME if invalid_function else name, arguments,
            ),
            error="; ".join(errors),
            raw_arguments=detached,
        ))
    return result


# 只识别协议专用前缀，不匹配通用 XML/HTML；末尾 > 可缺失以识别残缺标记。
_MARKER = re.compile(
    r"<\s*(?P<close>/\s*)?(?:[|｜]\s*)+(?P<inner_close>/\s*)?"
    r"(?P<body>(?:DSML\b|tool[▁_])[^<>]*)(?P<complete>>)?",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "«": "»",
                "「": "」", "『": "』"}
_QUOTE_CHARS = set(_QUOTE_PAIRS) | set(_QUOTE_PAIRS.values())
# 规范化拼写与 _MARKER 使用相同的空白及竖线规则。
_STARTS = (
    "<|dsml|calls>", "<|dsml|tool_calls>", "<|dsml|function_calls>",
    "<|dsml|invoke", "<|dsml|parameter",
    "<|tool▁calls▁begin|>", "<|tool▁calls▁end|>",
    "<|tool▁call▁begin|>", "<|tool▁call▁end|>", "<|tool▁sep|>",
    "<|tool▁outputs▁begin|>", "<|tool▁outputs▁end|>",
    "<|tool▁output▁begin|>", "<|tool▁output▁end|>",
    "<|tool_calls|>", "<|tool_call|>", "<|tool_sep|>",
    "<|tool_calls_begin|>", "<|tool_calls_end|>",
    "<|tool_call_begin|>", "<|tool_call_end|>",
)


@dataclass(frozen=True)
class _Token:
    start: int
    end: int
    key: str
    closing: bool
    complete: bool


def _canonical(fragment: str) -> str:
    fragment = re.sub(r"\s+", "", fragment).lower().replace("｜", "|")
    return re.sub(r"\|+", "|", fragment)


def _prefix(fragment: str) -> bool:
    if not fragment or ">" in fragment:
        return False
    canonical = _canonical(fragment)
    # 闭合形式共用相同词汇，兼容 / 位于竖线之前或之后。
    canonical = canonical.replace("</", "<", 1).replace("<|/", "<|", 1)
    return bool(canonical) and any(s.startswith(canonical) for s in _STARTS)


def _tokens(text: str) -> list[_Token]:
    tokens = []
    for match in _MARKER.finditer(text):
        body = match.group("body")
        closing = bool(match.group("close") or match.group("inner_close"))
        if body[:4].lower() == "dsml":
            tail = body[4:].lstrip("|｜ \t\r\n")
            word = re.match(r"[A-Za-z_]+", tail)
            key = "dsml:" + (word.group().lower() if word else "")
        else:
            body = re.sub(r"\s+", "", body).strip("|｜").lower()
            if body.startswith("tool_") or body.startswith("tool▁"):
                body = body.replace("▁", "_")
            if body.endswith("_end"):
                closing = True
                body = body[:-4]
            elif body.endswith("_begin"):
                body = body[:-6]
            key = "native:" + body
        tokens.append(_Token(match.start(), match.end(), key, closing,
                             bool(match.group("complete"))))
    # 末尾的协议单词尚未完整时也识别；裸 < 在 inspect 中保留，流式侧暂缓输出。
    start = text.rfind("<")
    if start >= 0 and _prefix(text[start:]) and "|" in text[start:].replace("｜", "|"):
        if not tokens or tokens[-1].start != start:
            tokens.append(_Token(start, len(text), "partial", False, False))
    return tokens


def _block_ends(
    tokens: list[_Token], protected: list[tuple[int, int]],
) -> dict[int, int]:
    # 按协议标记类型配对，避免对大量标记反复扫描整个后缀。
    stacks: dict[str, list[int]] = {}
    ends = {}
    for index, token in enumerate(tokens):
        if not token.complete or _inside(token.start, protected):
            continue
        stack = stacks.setdefault(token.key, [])
        if token.closing:
            ends[index] = token.end
            if stack:
                ends[stack.pop()] = token.end
        else:
            stack.append(index)
    return ends


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _inside(position: int, spans: list[tuple[int, int]]) -> bool:
    index = bisect_right(spans, (position, float("inf"))) - 1
    return index >= 0 and position < spans[index][1]


def _markdown_spans(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    spans = []
    continuable = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        if fence:
            char, width, start = fence
            if re.fullmatch(r" {0,3}" + re.escape(char) + r"{" + str(width)
                            + r",}[ \t]*(?:\r?\n)?", line):
                spans.append((start, end))
                fence = None
        elif re.match(r"(?: {0,3}>| {4}|\t)", line):
            spans.append((offset, end))
            continuable.append((offset, end))
        else:
            match = _FENCE.match(line)
            if match:
                run = match.group(1)
                fence = (run[0], len(run), offset)
        offset = end
    if fence:
        spans.append((fence[2], len(text)))
    spans = _merge(spans)

    # 行内代码使用等长反引号序列配对，不局限于单反引号。
    runs = list(re.finditer(r"`+", text))
    index = 0
    inline = []
    while index < len(runs):
        opening = runs[index]
        if _inside(opening.start(), spans):
            index += 1
            continue
        end_index = index + 1
        while end_index < len(runs):
            closing = runs[end_index]
            if _inside(closing.start(), spans):
                break
            if closing.group() == opening.group():
                inline.append((opening.start(), closing.end()))
                index = end_index
                break
            end_index += 1
        index += 1
    return _merge(spans + inline), _merge(continuable)


def _quote_start(text: str, position: int) -> tuple[int, str] | None:
    # 仅向前扫描同一行普通文字，不跨越其他标签；既支持直接引号包裹标记，
    # 也支持引号内先有示例说明，但不会把属性/JSON 中的引号误当整块引用。
    position -= 1
    while position >= 0:
        char = text[position]
        if char in "<>\r\n":
            break
        if char in _QUOTE_CHARS:
            if char not in _QUOTE_PAIRS:
                return None
            if position and text[position - 1] == "\\":
                return None
            if (char == "'" and position and position + 1 < len(text)
                    and text[position - 1].isascii() and text[position - 1].isalnum()
                    and text[position + 1].isascii() and text[position + 1].isalnum()):
                # 英文单词内部的撇号不是示例引号；中文说明后可紧接引号。
                return None
            return position, _QUOTE_PAIRS[char]
        position -= 1
    return None


def _protected_spans(text: str, tokens: list[_Token]) -> list[tuple[int, int]]:
    markdown, continuable = _markdown_spans(text)
    ends = _block_ends(tokens, [])
    quoted = []
    for index, token in enumerate(tokens):
        if _inside(token.start, markdown):
            # 引用/缩进首行内开启的完整示例块应整体保留，即使后续行没有
            # 重复缩进或 >；围栏和行内代码则不能向其显式边界之外延伸。
            if _inside(token.start, continuable) and not token.closing:
                end = ends.get(index)
                if end is not None:
                    quoted.append((token.start, end))
            continue
        opener = _quote_start(text, token.start)
        if not opener:
            continue
        start, quote = opener
        endpoints = [token.end] if token.complete else []
        block_end = ends.get(index)
        if block_end is not None:
            endpoints.append(block_end)
        for end in endpoints:
            while end < len(text) and text[end].isspace():
                end += 1
            if end < len(text) and text[end] == quote:
                quoted.append((start, end + 1))
                break
        else:
            # 明确引号包裹的残缺标记也是示例，但必须真是协议前缀。
            end = text.find(quote, token.start)
            if end >= 0 and _prefix(text[token.start:end]):
                quoted.append((start, end + 1))
    return _merge(markdown + quoted)


def inspect_tool_markup(text: str) -> tuple[str, bool]:
    """删除非引用的伪工具标记块，绝不提取可执行调用。

    完整外层块整块删除；残缺起始标记从起点截到正文末尾；孤立完整结束
    标记只剥除标记本身。Markdown 示例及明确引号包裹的示例原样保留。
    其余正文（包括空白）不变。True 只表示正文格式异常，不表示执行过工具。
    """
    tokens = _tokens(text)
    if not tokens:
        return text, False
    protected = _protected_spans(text, tokens)
    ends = _block_ends(tokens, protected)
    pieces = []
    cursor = 0
    detected = False
    for index, token in enumerate(tokens):
        if token.start < cursor or _inside(token.start, protected):
            continue
        pieces.append(text[cursor:token.start])
        detected = True
        if not token.complete:
            cursor = len(text)
        else:
            end = ends.get(index)
            cursor = end if end is not None else len(text)
    pieces.append(text[cursor:])
    return "".join(pieces), detected


def markup_prefix_pending(text: str) -> bool:
    """正文末尾是否可能是尚未收齐、且未被引用的协议标记头。

    仅作流式缓冲提示，不是调用检测或执行授权。末尾裸 < 也返回 True；
    inspect 只有见到协议竖线后才把残缺前缀视作异常。已完成的标记头
    由 inspect 处理，不属于此函数的 pending 范围。
    """
    start = text.rfind("<")
    if start < 0:
        return False
    tokens = _tokens(text)
    if _inside(start, _protected_spans(text, tokens)):
        return False
    return _prefix(text[start:]) or any(
        token.start == start and not token.complete for token in tokens
    )
