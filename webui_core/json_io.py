# -*- coding: utf-8 -*-
"""Fault-tolerant JSON file IO for the WebUI."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_WRITE_JSON_BAK_KEEP = 5


def read_json(path: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        try:
            backups = sorted(
                path.parent.glob(f"{path.name}.*.bak"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for bak in backups:
                try:
                    with bak.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    print(f"⚠️ read_json {path} 解析失败({e})，已从备份 {bak.name} 恢复读取。")
                    return data
                except Exception:
                    continue
        except Exception:
            pass
        print(f"⚠️ read_json {path} 解析失败且无可用备份，使用默认值。错误：{e}")
        return default


def _prune_old_backups(path: Path, keep: int = _WRITE_JSON_BAK_KEEP) -> None:
    """只保留最近 keep 份 JSON 备份。"""
    try:
        backups = sorted(
            path.parent.glob(f"{path.name}.*.bak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[keep:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def atomic_write_text(path: Path, text: str) -> None:
    """在目标文件同目录原子替换，避免中断留下半文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + f".{datetime.now().strftime('%Y%m%d%H%M%S_%f')}.bak")
    if path.exists():
        atomic_write_text(backup, path.read_text(encoding="utf-8", errors="replace"))
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=4) + "\n")
    _prune_old_backups(path)
