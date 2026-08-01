# -*- coding: utf-8 -*-
"""Chat memory persistence (private / group JSON files)."""
from __future__ import annotations

import json
import os
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
        try:
            os.makedirs(self.memory_path, exist_ok=True)
        except Exception:
            pass

    def _get_private_filename(self, user_id: int) -> str:
        return os.path.join(self.memory_path, f"private_{user_id}.json")

    def _get_group_filename(self, group_id: int) -> str:
        return os.path.join(self.memory_path, f"group_{group_id}.json")

    def save_private_memory(self, user_id: int, history: list, token_counter: int = 0):
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
            atomic_write_json(file_path, data, indent=2)
            return True
        except Exception:
            return False

    def load_private_memory(self, user_id: int) -> tuple[list, int]:
        """加载私聊记忆"""
        try:
            file_path = self._get_private_filename(user_id)
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                history = data.get("history", [])
                history = [msg for msg in history if msg.get("role") in ["user", "assistant"]]
                token_counter = data.get("token_counter", 0)
                return history, token_counter
        except Exception:
            pass
        return [], 0

    def save_group_memory(self, group_id: int, history: list, token_counter: int = 0, group_roles: dict = None):
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
            atomic_write_json(file_path, data, indent=2)
            return True
        except Exception:
            return False

    def load_group_memory(self, group_id: int) -> tuple[list, int, dict]:
        """加载群聊记忆"""
        try:
            file_path = self._get_group_filename(group_id)
            if os.path.exists(file_path):
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
            if os.path.exists(file_path):
                os.remove(file_path)
                return True
        except Exception:
            pass
        return False

    def delete_group_memory(self, group_id: int):
        try:
            file_path = self._get_group_filename(group_id)
            if os.path.exists(file_path):
                os.remove(file_path)
                return True
        except Exception:
            pass
        return False

    def get_all_sessions(self) -> dict:
        sessions = {"private": [], "group": []}
        try:
            for filename in os.listdir(self.memory_path):
                if filename.startswith("private_") and filename.endswith(".json"):
                    user_id = filename.replace("private_", "").replace(".json", "")
                    sessions["private"].append(int(user_id))
                elif filename.startswith("group_") and filename.endswith(".json"):
                    group_id = filename.replace("group_", "").replace(".json", "")
                    sessions["group"].append(int(group_id))
        except Exception:
            pass
        return sessions
