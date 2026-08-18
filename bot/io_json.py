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

    锁文件用完**不删除**。这是经典的 unlink-lockfile 缺陷：Linux 下 unlink 会成功，
    进程 A 删掉锁文件后，进程 C 用同名路径新建会拿到另一个 inode，于是 B 和 C 的
    flock 作用在两个不同 inode 上，互斥彻底失效。Windows 上 unlink 会因句柄占用
    失败、异常被吞掉，反而侥幸保住了互斥——两个平台行为不一致更难排查。
    留一个空锁文件的代价可以忽略。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_file = target.with_suffix(target.suffix + ".lock")

    # 跨平台文件锁
    if sys.platform == "win32":
        import msvcrt
        # 用 a 而不是 w：w 会截断文件，多进程同时打开时可能互相清空
        lock_fd = open(lock_file, "a")
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
    else:
        import fcntl
        lock_fd = open(lock_file, "a")
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
