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


def compare_versions(current: str, latest: str) -> int:
    a = parse_version_parts(current)
    b = parse_version_parts(latest)
    max_len = max(len(a), len(b))
    for i in range(max_len):
        av = a[i] if i < len(a) else 0
        bv = b[i] if i < len(b) else 0
        if type(av) is type(bv):
            if av < bv:
                return -1
            if av > bv:
                return 1
            continue
        avs, bvs = str(av), str(bv)
        if avs < bvs:
            return -1
        if avs > bvs:
            return 1
    return 0
