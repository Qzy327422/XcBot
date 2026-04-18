# key_manager.py
import threading
import time
from typing import List, Tuple, Optional, Dict
from collections import deque


class SiliconFlowKeyManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, endpoints: List[Dict] = None):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, endpoints: List[Dict] = None):
        if self._initialized:
            return
        with self._lock:
            if not self._initialized:
                self.endpoints = endpoints or []
                self.current_index = 0
                self.last_selected_index = None
                self.key_list = []
                self.switch_logs = deque(maxlen=200)

                self.default_index = None
                self.default_model = None

                self._initialized = True
                self._build_key_list()

    def set_endpoints(self, endpoints: List[Dict]):
        with self._lock:
            self.endpoints = endpoints or []
            self.current_index = 0
            self.last_selected_index = None
            self.switch_logs.clear()
            self.default_index = None
            self.default_model = None
            self._build_key_list()

    def _build_key_list(self):
        self.key_list = []
        idx = 1
        for endpoint in self.endpoints:
            base_url = endpoint["base_url"]
            model = endpoint.get("model", "deepseek-ai/DeepSeek-V3.2")
            for key in endpoint.get("keys", []):
                self.key_list.append({
                    "id": idx,
                    "base_url": base_url,
                    "key": key,
                    "model": model,
                    "supports_multimodal": bool(endpoint.get("supports_multimodal", False)),
                    "fail_count": 0,
                    "cooldown_until": 0.0,
                    "disabled": False,
                    "last_error": "",
                    "last_used_at": 0.0,
                })
                idx += 1

    def _mask_key(self, key: str) -> str:
        if not key:
            return ""
        if len(key) <= 12:
            return key
        return f"{key[:8]}...{key[-4:]}"

    def _now(self) -> float:
        return time.time()

    def _find_index_by_key(self, key: str = None) -> Optional[int]:
        if key:
            for i, item in enumerate(self.key_list):
                if item["key"] == key:
                    return i

        idx = self.last_selected_index
        if idx is not None and 0 <= idx < len(self.key_list):
            return idx

        return None

    def _is_available(self, item: Dict) -> bool:
        return (not item["disabled"]) and item["cooldown_until"] <= self._now()

    def _find_default_available_index(self) -> Optional[int]:
        if self.default_index is not None:
            idx = self.default_index - 1
            if 0 <= idx < len(self.key_list):
                item = self.key_list[idx]
                if self._is_available(item):
                    return idx

        if self.default_model:
            for i, item in enumerate(self.key_list):
                if item["model"] == self.default_model and self._is_available(item):
                    return i

        return None

    def _find_next_available_from(self, start_index: int) -> Optional[int]:
        total = len(self.key_list)
        if total == 0:
            return None

        for offset in range(total):
            idx = (start_index + offset) % total
            item = self.key_list[idx]
            if self._is_available(item):
                return idx
        return None

    def get_current(self, require_multimodal: bool = False) -> Optional[Tuple[str, str, str, bool]]:
        with self._lock:
            if not self.key_list:
                return None

            def _matches_request(item: Dict) -> bool:
                if require_multimodal and not bool(item.get("supports_multimodal", False)):
                    return False
                return True

            default_idx = self._find_default_available_index()
            if default_idx is not None:
                if self.current_index != default_idx:
                    old = self.current_index + 1 if self.key_list else 0
                    self.current_index = default_idx
                    self.switch_logs.append({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "from": old,
                        "to": default_idx + 1,
                        "reason": "auto switch back to default",
                        "manual": False
                    })

                self.last_selected_index = self.current_index
                item = self.key_list[self.current_index]
                if _matches_request(item):
                    item["last_used_at"] = self._now()
                    return item["base_url"], item["key"], item["model"], bool(item.get("supports_multimodal", False))

            next_idx = self._find_next_available_from(self.current_index)
            if next_idx is None:
                return None

            if require_multimodal:
                matched_idx = None
                total = len(self.key_list)
                for offset in range(total):
                    idx = (self.current_index + offset) % total
                    item = self.key_list[idx]
                    if self._is_available(item) and _matches_request(item):
                        matched_idx = idx
                        break
                if matched_idx is None:
                    return None
                next_idx = matched_idx

            if self.current_index != next_idx:
                old = self.current_index + 1 if self.key_list else 0
                self.current_index = next_idx
                self.switch_logs.append({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": old,
                    "to": next_idx + 1,
                    "reason": "auto failover switch",
                    "manual": False
                })

            self.last_selected_index = self.current_index
            item = self.key_list[self.current_index]
            item["last_used_at"] = self._now()
            return item["base_url"], item["key"], item["model"], bool(item.get("supports_multimodal", False))

    def get_next_for_request(self, tried_keys: set[str] = None, include_cooldown: bool = True,
                             require_multimodal: bool = False) -> Optional[Tuple[str, str, str, bool]]:
        """为单次请求选择下一个可尝试的 key。

        - 优先遵循 default_index / default_model
        - 按 key 维度排除本轮已经尝试过的 key
        - include_cooldown=True 时允许本轮继续尝试处于冷却中的 key
        - disabled 的 key 不参与尝试
        - require_multimodal=True 时仅返回支持多模态的接口
        """
        with self._lock:
            if not self.key_list:
                return None

            tried_keys = tried_keys or set()
            total = len(self.key_list)
            now = self._now()

            def _request_allowed(item: Dict, allow_text_fallback: bool = False) -> bool:
                if item["disabled"]:
                    return False
                if item["key"] in tried_keys:
                    return False
                if require_multimodal and not allow_text_fallback and not bool(item.get("supports_multimodal", False)):
                    return False
                if (not include_cooldown) and item["cooldown_until"] > now:
                    return False
                return True

            default_idx = self._find_default_available_index()
            if default_idx is not None:
                default_item = self.key_list[default_idx]
                # 默认/手动选中的 API 优先级最高。图片请求时如果默认 API 不支持多模态，
                # 返回该 API 并由上层按纯文本发送，避免“已切换到 API-2 但图片又跳回 API-1”。
                if _request_allowed(default_item, allow_text_fallback=True):
                    self.current_index = default_idx
                    self.last_selected_index = default_idx
                    default_item["last_used_at"] = now
                    return default_item["base_url"], default_item["key"], default_item["model"], bool(default_item.get("supports_multimodal", False))

            for offset in range(total):
                idx = (self.current_index + offset) % total
                item = self.key_list[idx]
                if not _request_allowed(item):
                    continue
                self.current_index = idx
                self.last_selected_index = idx
                item["last_used_at"] = now
                return item["base_url"], item["key"], item["model"], bool(item.get("supports_multimodal", False))

            # 多模态接口都不可用时，降级到纯文本接口继续本轮请求，避免图片请求直接失败。
            if require_multimodal:
                for offset in range(total):
                    idx = (self.current_index + offset) % total
                    item = self.key_list[idx]
                    if item["disabled"] or item["key"] in tried_keys:
                        continue
                    if (not include_cooldown) and item["cooldown_until"] > now:
                        continue
                    self.current_index = idx
                    self.last_selected_index = idx
                    item["last_used_at"] = now
                    return item["base_url"], item["key"], item["model"], bool(item.get("supports_multimodal", False))

            return None

    def mark_success(self, key: str = None):
        with self._lock:
            idx = self._find_index_by_key(key)
            if idx is None:
                return

            item = self.key_list[idx]
            item["last_used_at"] = self._now()
            self.current_index = idx
            self.last_selected_index = idx

    def mark_failure(self, key: str = None, reason: str = "", cooldown_seconds: int = 1):
        with self._lock:
            if not self.key_list:
                return

            idx = self._find_index_by_key(key)
            if idx is None:
                return

            item = self.key_list[idx]
            item["fail_count"] += 1
            item["last_error"] = reason
            item["cooldown_until"] = self._now() + max(1, cooldown_seconds)

            next_index = (idx + 1) % len(self.key_list)
            self.current_index = next_index

            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": idx + 1,
                "to": next_index + 1,
                "reason": f"failure: {reason}, cooldown={cooldown_seconds}s",
                "manual": False
            })

    def disable_key(self, key: str = None, reason: str = ""):
        with self._lock:
            if not self.key_list:
                return

            idx = self._find_index_by_key(key)
            if idx is None:
                return

            item = self.key_list[idx]
            item["disabled"] = True
            item["last_error"] = reason
            item["cooldown_until"] = 0.0

            next_index = (idx + 1) % len(self.key_list)
            self.current_index = next_index

            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": idx + 1,
                "to": next_index + 1,
                "reason": f"disabled: {reason}",
                "manual": False
            })

    def enable_key(self, index: int) -> bool:
        with self._lock:
            if 1 <= index <= len(self.key_list):
                item = self.key_list[index - 1]
                item["disabled"] = False
                item["cooldown_until"] = 0.0
                item["last_error"] = ""
                self.switch_logs.append({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": self.current_index + 1 if self.key_list else 0,
                    "to": index,
                    "reason": "manual enable key",
                    "manual": True
                })
                return True
            return False

    def manual_switch_by_index(self, index: int) -> bool:
        with self._lock:
            if 1 <= index <= len(self.key_list):
                old = self.current_index + 1 if self.key_list else 0
                self.current_index = index - 1
                self.last_selected_index = self.current_index
                self.default_index = index
                self.default_model = None
                self.switch_logs.append({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": old,
                    "to": index,
                    "reason": "manual switch by index and set as active default",
                    "manual": True
                })
                return True
            return False

    def manual_switch_by_model(self, model: str) -> bool:
        with self._lock:
            old = self.current_index + 1 if self.key_list else 0
            for i, item in enumerate(self.key_list):
                if item["model"] == model and not item["disabled"]:
                    self.current_index = i
                    self.last_selected_index = i
                    self.default_model = model
                    self.default_index = None
                    self.switch_logs.append({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "from": old,
                        "to": i + 1,
                        "reason": f"manual switch by model and set as active default: {model}",
                        "manual": True
                    })
                    return True
            return False

    def set_default_by_index(self, index: int) -> bool:
        with self._lock:
            if 1 <= index <= len(self.key_list):
                self.default_index = index
                self.default_model = None
                self.current_index = index - 1
                self.last_selected_index = self.current_index
                self.switch_logs.append({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": self.current_index + 1 if self.key_list else 0,
                    "to": index,
                    "reason": f"set default by index: {index}",
                    "manual": True
                })
                return True
            return False

    def set_default_by_model(self, model: str) -> bool:
        with self._lock:
            target_index = None
            for i, item in enumerate(self.key_list):
                if item["model"] == model:
                    target_index = i
                    break
            if target_index is not None:
                self.default_model = model
                self.default_index = None
                old = self.current_index + 1 if self.key_list else 0
                self.current_index = target_index
                self.last_selected_index = target_index
                self.switch_logs.append({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": old,
                    "to": target_index + 1,
                    "reason": f"set default by model: {model}",
                    "manual": True
                })
                return True
            return False

    def clear_default(self):
        with self._lock:
            self.default_index = None
            self.default_model = None
            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": self.current_index + 1 if self.key_list else 0,
                "to": 0,
                "reason": "clear default target",
                "manual": True
            })

    def reset_cooldown(self, index: int) -> bool:
        with self._lock:
            if 1 <= index <= len(self.key_list):
                item = self.key_list[index - 1]
                item["cooldown_until"] = 0.0
                item["last_error"] = ""
                self.switch_logs.append({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": self.current_index + 1 if self.key_list else 0,
                    "to": index,
                    "reason": "manual reset cooldown",
                    "manual": True
                })
                return True
            return False

    def is_default_key(self, key: str = None) -> bool:
        with self._lock:
            idx = self._find_index_by_key(key)
            if idx is None:
                return False

            if self.default_index is not None:
                return idx == (self.default_index - 1)

            if self.default_model is not None:
                return self.key_list[idx]["model"] == self.default_model

            return False

    def get_key_info(self, key: str = None) -> Optional[Dict]:
        with self._lock:
            idx = self._find_index_by_key(key)
            if idx is None:
                return None
            return self.key_list[idx].copy()

    def get_all_keys(self) -> List[str]:
        with self._lock:
            return [item["key"] for item in self.key_list]

    def get_status_list(self) -> List[Dict]:
        with self._lock:
            now = self._now()
            result = []

            for i, item in enumerate(self.key_list, start=1):
                if item["disabled"]:
                    status = "disabled"
                elif item["cooldown_until"] > now:
                    left = int(item["cooldown_until"] - now)
                    status = f"cooldown({left}s)"
                else:
                    status = "active"

                is_default = False
                if self.default_index is not None and self.default_index == i:
                    is_default = True
                elif self.default_model is not None and self.default_model == item["model"]:
                    is_default = True

                result.append({
                    "id": i,
                    "api_name": f"API-{i} | {self._mask_key(item['key'])}",
                    "base_url": item["base_url"],
                    "model": item["model"],
                    "key": self._mask_key(item["key"]),
                    "status": status,
                    "fail_count": item["fail_count"],
                    "last_error": item["last_error"],
                    "last_used_at": item["last_used_at"],
                    "is_current": (i - 1 == self.current_index),
                    "is_default": is_default
                })
            return result

    def get_switch_logs(self, limit: int = 20) -> List[Dict]:
        with self._lock:
            return list(self.switch_logs)[-limit:]

    def get_current_display(self) -> str:
        with self._lock:
            if not self.key_list:
                return "无可用 API"
            item = self.key_list[self.current_index]
            return f"API-{self.current_index + 1} | Key: {self._mask_key(item['key'])} | Model: {item['model']} | Endpoint: {item['base_url']}"

    def get_default_display(self) -> str:
        with self._lock:
            if self.default_index is not None:
                if 1 <= self.default_index <= len(self.key_list):
                    item = self.key_list[self.default_index - 1]
                    return f"默认 API-{self.default_index} | Key: {self._mask_key(item['key'])} | Model: {item['model']} | Endpoint: {item['base_url']}"
                return f"默认编号: {self.default_index}（无效）"

            if self.default_model is not None:
                return f"默认模型: {self.default_model}"

            return "未设置默认模型"


key_manager = SiliconFlowKeyManager()
