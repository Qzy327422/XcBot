# -*- coding: utf-8 -*-
"""Version parsing and comparison helpers."""
from __future__ import annotations

import re
from typing import Any


def parse_version_parts(value: str) -> list[Any]:
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


# 预发布标识。数字段相同时，带这些标识的版本一律低于不带的正式版。
_PRERELEASE_ORDER = {
    "dev": 0, "snapshot": 0, "nightly": 0,
    "alpha": 1, "a": 1,
    "beta": 2, "b": 2,
    "pre": 3, "preview": 3,
    "rc": 4, "c": 4,
}


def _split_release(parts: list) -> tuple[list, tuple]:
    """把版本片段拆成「数字主体」和「预发布权重」。

    v3.0-alpha 的片段是 [3, 0, 'alpha']。主体必须只取 [3, 0]，否则拿 'alpha'
    去和正式版补位的 0 比较——代码走字符串分支，"alpha" > "0" 成立，
    于是得出「v3.0-alpha 比 v3.0 新」的错误结论，更新检查会把正式版当旧版。
    """
    numeric: list[int] = []
    tag_rank = None
    tag_num = 0
    for item in parts:
        if isinstance(item, int):
            if tag_rank is None:
                numeric.append(item)
            else:
                tag_num = item          # rc1 里的那个 1
                break
        elif tag_rank is None and item in _PRERELEASE_ORDER:
            tag_rank = _PRERELEASE_ORDER[item]
        else:
            # 无法识别的字母段（例如 post、hotfix）不参与比较
            break
    # 正式版的权重高于任何预发布
    return numeric, (99 if tag_rank is None else tag_rank, tag_num)


def compare_versions(current: str, latest: str) -> int:
    a_num, a_tag = _split_release(parse_version_parts(current))
    b_num, b_tag = _split_release(parse_version_parts(latest))
    for i in range(max(len(a_num), len(b_num))):
        av = a_num[i] if i < len(a_num) else 0
        bv = b_num[i] if i < len(b_num) else 0
        if av != bv:
            return -1 if av < bv else 1
    if a_tag != b_tag:
        return -1 if a_tag < b_tag else 1
    return 0
