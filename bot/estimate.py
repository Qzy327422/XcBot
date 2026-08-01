# -*- coding: utf-8 -*-
"""Token estimation for compression / summary thresholds only."""


def estimate_tokens(text: str) -> int:
    """估算 Token 数量（仅用于压缩/总结阈值，不替代 API usage）。

    规则尽量贴近主流 BPE 的经验比例：
    - CJK 汉字/全角符号 ≈ 1.5 token/字（按 3/2 计）
    - 拉丁字母/数字串按词块粗估（约 4 字符 1 token）
    - 空白与标点单独计少量开销
    """
    if not text:
        return 0
    cjk = 0
    latin_run = 0
    other = 0
    for ch in text:
        code = ord(ch)
        if (
            0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF
            or 0xF900 <= code <= 0xFAFF
            or 0x3000 <= code <= 0x303F
            or 0xFF00 <= code <= 0xFFEF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
        ):
            cjk += 1
        elif ch.isascii() and (ch.isalnum() or ch in ("_", "-", ".", "/", ":", "@")):
            latin_run += 1
        else:
            other += 1
    # 汉字按 1.5 token，拉丁按 4 字符 1 token，其它按 1 token
    tokens = (cjk * 3 + 1) // 2 + (latin_run + 3) // 4 + other
    return max(1, tokens)
