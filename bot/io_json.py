# -*- coding: utf-8 -*-
"""Atomic JSON write helpers."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def atomic_write_json(path, data, *, indent=2) -> None:
    """在目标文件同目录原子写入 JSON，避免进程中断留下半文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
