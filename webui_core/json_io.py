# -*- coding: utf-8 -*-
"""Fault-tolerant JSON file IO for the WebUI."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_WRITE_JSON_BAK_KEEP = 5

# 备份统一收进被备份文件同级的 config_backup/ 子目录，不散在根目录里。
# 目录名与 config_migrate.ensure_config_up_to_date 的默认备份目录一致，
# 两套备份机制共用一个文件夹；那边的文件名前缀是 config-before-auto-upgrade_，
# 与这里的 <name>.<时间戳>.bak 不冲突，互相不会被对方的清理规则删掉。
_BACKUP_DIR_NAME = "config_backup"


def backup_dir_for(path: Path) -> Path:
    return Path(path).parent / _BACKUP_DIR_NAME


def _backup_glob(path: Path) -> str:
    return f"{Path(path).name}.*.bak"


def _iter_backups(path: Path) -> "list[Path]":
    """按新鲜度倒序列出可用备份。

    同时扫描新目录与旧的同级位置：升级前产生的 .bak 仍在根目录，
    只认新目录会让老备份在最需要它们的时候（配置刚损坏）失效。
    """
    path = Path(path)
    found: "list[Path]" = []
    for root in (backup_dir_for(path), path.parent):
        try:
            found.extend(root.glob(_backup_glob(path)))
        except Exception:
            continue
    # 同一文件可能被两个 root 各命中一次（新目录恰好是 path.parent 的子目录时不会，
    # 但调用方传入的路径本身可能已在 config_backup/ 内），按真实路径去重。
    unique = {p.resolve(): p for p in found}
    return sorted(unique.values(), key=lambda p: p.stat().st_mtime, reverse=True)


# 按文件路径的事务锁。原子替换只保证不出现半截文件，防不住
# 「读旧对象 → 改不同字段 → 整体写回」这种丢更新：两个线程各读一份旧配置，
# 各改一个字段再写回，后写的会把前一个人的改动整段抹掉。
# 需要事务保护的调用方用 config_transaction() 包住整段读改写。
_file_locks: "dict[str, threading.Lock]" = {}
_file_locks_guard = threading.Lock()


def file_lock(path) -> "threading.Lock":
    key = str(path).casefold()
    with _file_locks_guard:
        lock = _file_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _file_locks[key] = lock
        return lock


@contextmanager
def config_transaction(path):
    """把一次完整的读-改-写包成事务。同一文件的并发事务会排队。"""
    lock = file_lock(path)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


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
            for bak in _iter_backups(path):
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
    """只保留最近 keep 份 JSON 备份（含旧的根目录残留）。"""
    try:
        for old in _iter_backups(path)[keep:]:
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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S_%f")
        backup = backup_dir_for(path) / f"{path.name}.{stamp}.bak"
        # 备份失败不能挡住正常保存：目录建不出来（权限、磁盘满）时
        # 让本次写入照常进行，只是这一次没有回退点。
        try:
            atomic_write_text(backup, path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            print(f"⚠️ 备份 {path.name} 失败，本次保存不生成回退点：{e}")
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=4) + "\n")
    _prune_old_backups(path)
