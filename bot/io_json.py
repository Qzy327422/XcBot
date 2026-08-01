# -*- coding: utf-8 -*-
"""Atomic JSON write helpers with file locking."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path


def atomic_write_json(path, data, *, indent=2) -> None:
    """在目标文件同目录原子写入 JSON，带文件锁防止并发写入冲突。

    使用跨平台文件锁：
    - Windows: msvcrt.locking
    - Unix/Linux: fcntl.flock

    锁定策略：在同一文件 + ".lock" 上加排他锁，避免多进程同时写入。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_file = target.with_suffix(target.suffix + ".lock")

    # 跨平台文件锁
    if sys.platform == "win32":
        import msvcrt
        lock_fd = open(lock_file, "w")
        try:
            # Windows: 独占锁（LK_NBLCK 会立即失败，LK_LOCK 会阻塞等待）
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
            _do_atomic_write(target, data, indent)
        finally:
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            lock_fd.close()
            try:
                lock_file.unlink()
            except Exception:
                pass
    else:
        import fcntl
        lock_fd = open(lock_file, "w")
        try:
            # Unix: 排他锁（LOCK_EX 会阻塞等待）
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            _do_atomic_write(target, data, indent)
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            lock_fd.close()
            try:
                lock_file.unlink()
            except Exception:
                pass


def _do_atomic_write(target: Path, data, indent: int) -> None:
    """实际的原子写入逻辑（不含锁）。"""
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
