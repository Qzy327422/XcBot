# -*- coding: utf-8 -*-
"""Chat memory persistence (private / group JSON files)."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict

from bot.io_json import atomic_write_json
from bot.paths import BASE_DIR


class ChatMemoryManager:
    """聊天记忆管理器 - 每个会话独立存储，只存储对话历史，不存系统提示词"""

    def __init__(self, memory_path: str = None):
        self.private_chats: Dict[int, dict] = {}
        self.group_chats: Dict[int, dict] = {}
        self.memory_path = memory_path or os.path.join(str(BASE_DIR), "data", "ai_memory")
        # 按会话文件分别加锁。上层的 _history_lock 保护的是内存里的 history，
        # 落盘是另一层：LRU 淘汰（_evict）、退出时的 save_all_ai_memories 都不持
        # 那把锁，同一会话可能被并发写入。atomic_write_json 的文件锁只保证不产生
        # 半截文件，但先写后写的内容仍会互相覆盖，丢掉一轮对话。
        self._file_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        try:
            os.makedirs(self.memory_path, exist_ok=True)
        except Exception:
            pass

    def _file_lock(self, file_path: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._file_locks.get(file_path)
            if lock is None:
                lock = threading.Lock()
                self._file_locks[file_path] = lock
            return lock

    def _get_private_filename(self, user_id: int) -> str:
        return os.path.join(self.memory_path, f"private_{user_id}.json")

    def _get_group_filename(self, group_id: int) -> str:
        return os.path.join(self.memory_path, f"group_{group_id}.json")

    def save_private_memory(self, user_id: int, history: list, token_counter: int = 0,
                            should_write=None):
        """保存私聊记忆"""
        try:
            file_path = self._get_private_filename(user_id)
            clean_history = [msg for msg in history if msg.get("role") in ["user", "assistant"]]
            data = {
                "user_id": user_id,
                "history": clean_history,
                "token_counter": token_counter,
                "save_time": time.time(),
                "version": "2.1",
            }
            with self._file_lock(file_path):
                # 拿到文件锁之后再确认一次「这份数据还该写吗」。
                # /reset 可能在我们组装 data 的这段时间里把会话删掉了，
                # 只在进锁前判断的话旧快照仍会被写回去，记忆复活。
                if callable(should_write) and not should_write():
                    return False
                atomic_write_json(file_path, data, indent=2)
            return True
        except Exception:
            return False

    def load_private_memory(self, user_id: int) -> tuple[list, int]:
        """加载私聊记忆"""
        try:
            file_path = self._get_private_filename(user_id)
            with self._file_lock(file_path):
                if not os.path.exists(file_path):
                    return [], 0
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            history = data.get("history", [])
            history = [msg for msg in history if msg.get("role") in ["user", "assistant"]]
            token_counter = data.get("token_counter", 0)
            return history, token_counter
        except Exception:
            pass
        return [], 0

    def save_group_memory(self, group_id: int, history: list, token_counter: int = 0,
                          group_roles: dict = None, should_write=None):
        """保存群聊记忆"""
        try:
            file_path = self._get_group_filename(group_id)
            clean_history = [msg for msg in history if msg.get("role") in ["user", "assistant"]]
            data = {
                "group_id": group_id,
                "history": clean_history,
                "token_counter": token_counter,
                "group_roles": group_roles or {},
                "save_time": time.time(),
                "version": "2.1",
            }
            with self._file_lock(file_path):
                if callable(should_write) and not should_write():
                    return False
                atomic_write_json(file_path, data, indent=2)
            return True
        except Exception:
            return False

    def load_group_memory(self, group_id: int) -> tuple[list, int, dict]:
        """加载群聊记忆"""
        try:
            file_path = self._get_group_filename(group_id)
            with self._file_lock(file_path):
                if not os.path.exists(file_path):
                    return [], 0, {}
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            history = data.get("history", [])
            history = [msg for msg in history if msg.get("role") in ["user", "assistant"]]
            token_counter = data.get("token_counter", 0)
            group_roles = data.get("group_roles", {})
            return history, token_counter, group_roles
        except Exception:
            pass
        return [], 0, {}

    def delete_private_memory(self, user_id: int):
        try:
            file_path = self._get_private_filename(user_id)
            with self._file_lock(file_path):
                if os.path.exists(file_path):
                    os.remove(file_path)
                    return True
        except Exception:
            pass
        return False

    def delete_group_memory(self, group_id: int):
        try:
            file_path = self._get_group_filename(group_id)
            with self._file_lock(file_path):
                if os.path.exists(file_path):
                    os.remove(file_path)
                    return True
        except Exception:
            pass
        return False

    def get_all_sessions(self) -> dict:
        """扫描记忆目录列出所有会话。

        每个文件单独 try：目录里可能混入写入中断残留的 .xxx.tmp、手工改名的
        private_123_bak.json、或 .lock 文件，只要有一个文件名转不成整数，
        整个循环就会被外层 except 打断、后面的会话全部漏掉。
        """
        sessions = {"private": [], "group": []}
        try:
            filenames = os.listdir(self.memory_path)
        except Exception:
            return sessions

        for filename in filenames:
            if not filename.endswith(".json"):
                continue
            try:
                if filename.startswith("private_"):
                    sessions["private"].append(int(filename[len("private_"):-len(".json")]))
                elif filename.startswith("group_"):
                    sessions["group"].append(int(filename[len("group_"):-len(".json")]))
            except (ValueError, TypeError):
                continue
        return sessions
