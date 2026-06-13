# key_manager.py
import threading
import time
from typing import List, Tuple, Optional, Dict
from collections import deque, defaultdict


class SiliconFlowKeyManager:
    _instance = None
    _lock = threading.RLock()

    def __new__(cls, endpoints: List[Dict] = None):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, endpoints: List[Dict] = None):
        if self._initialized:
            if endpoints is not None:
                self.set_endpoints(endpoints)
            return
        with self._lock:
            if not self._initialized:
                self.endpoints = endpoints or []
                self.current_index = 0
                self.last_selected_index = None
                self.key_list = []
                self.model_slots = []
                self.model_cursor = defaultdict(int)
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
            self.key_list = []
            self.model_slots = []
            self.model_cursor = defaultdict(int)
            self.switch_logs.clear()
            self.default_index = None
            self.default_model = None
            self._build_key_list()

    def _normalize_bool(self, value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return bool(default)
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on", "启用", "开启", "是"}:
            return True
        if text in {"0", "false", "no", "n", "off", "禁用", "关闭", "否"}:
            return False
        return bool(default)

    def _normalize_timeout(self, value, default: int = 60) -> int:
        try:
            seconds = int(float(value))
        except (TypeError, ValueError):
            seconds = int(default)
        return max(1, seconds)

    def _display_model(self, endpoint: Dict) -> str:
        provider_id = str(endpoint.get("provider_id", "") or "").strip()
        model = str(endpoint.get("model", "") or "").strip()
        display = str(endpoint.get("display_model", "") or "").strip()
        if display:
            return display
        return f"{provider_id}/{model}" if provider_id else model

    def _build_key_list(self):
        self.key_list = []
        self.model_slots = []
        idx = 1
        for rotation_index, endpoint in enumerate(self.endpoints):
            base_url = str(endpoint.get("base_url", "") or "").strip()
            model = str(endpoint.get("model", "") or "").strip() or "deepseek-chat"
            provider_id = str(endpoint.get("provider_id", "") or "").strip()
            display_model = self._display_model({**endpoint, "model": model, "provider_id": provider_id})
            timeout_seconds = self._normalize_timeout(endpoint.get("timeout_seconds", 60), 60)
            slot_indices = []
            for key in endpoint.get("keys", []):
                key = str(key or "").strip()
                if not key:
                    continue
                self.key_list.append({
                    "id": idx,
                    "provider_id": provider_id,
                    "base_url": base_url,
                    "key": key,
                    "model": model,
                    "display_model": display_model,
                    "supports_multimodal": self._normalize_bool(endpoint.get("supports_multimodal", False), default=False),
                    "timeout_seconds": timeout_seconds,
                    "rotation_index": rotation_index,
                    "fail_count": 0,
                    "cooldown_until": 0.0,
                    "disabled": False,
                    "last_error": "",
                    "last_used_at": 0.0,
                })
                slot_indices.append(len(self.key_list) - 1)
                idx += 1
            if slot_indices:
                self.model_slots.append({
                    "rotation_index": rotation_index,
                    "provider_id": provider_id,
                    "model": model,
                    "display_model": display_model,
                    "indices": slot_indices,
                })

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

    def _is_available(self, item: Dict, include_cooldown: bool = True) -> bool:
        if item.get("disabled"):
            return False
        if include_cooldown and item.get("cooldown_until", 0.0) > self._now():
            return False
        return True

    def _matches_request(self, item: Dict, require_multimodal: bool = False) -> bool:
        if require_multimodal and not bool(item.get("supports_multimodal", False)):
            return False
        return True

    def _result_tuple(self, item: Dict) -> Tuple[str, str, str, bool, int, str]:
        return (
            item["base_url"],
            item["key"],
            item["model"],
            bool(item.get("supports_multimodal", False)),
            int(item.get("timeout_seconds", 60) or 60),
            item.get("display_model") or item.get("model") or "",
        )

    def _pick_from_slot(self, slot: Dict, tried_keys: set[str], include_cooldown: bool, require_multimodal: bool) -> Optional[int]:
        indices = slot.get("indices") or []
        if not indices:
            return None
        start = int(self.model_cursor.get(slot["rotation_index"], 0) or 0) % len(indices)
        for offset in range(len(indices)):
            pos = (start + offset) % len(indices)
            idx = indices[pos]
            item = self.key_list[idx]
            if item["key"] in tried_keys:
                continue
            if not self._is_available(item, include_cooldown=include_cooldown):
                continue
            if not self._matches_request(item, require_multimodal=require_multimodal):
                continue
            self.model_cursor[slot["rotation_index"]] = (pos + 1) % len(indices)
            return idx
        return None

    def get_current(self, require_multimodal: bool = False) -> Optional[Tuple[str, str, str, bool, int, str]]:
        with self._lock:
            if not self.key_list or not self.model_slots:
                return None
            slot_pos = self.current_index if 0 <= self.current_index < len(self.model_slots) else 0
            slot = self.model_slots[slot_pos]
            idx = self._pick_from_slot(slot, set(), True, require_multimodal)
            if idx is not None:
                item = self.key_list[idx]
                item["last_used_at"] = self._now()
                self.last_selected_index = idx
                return self._result_tuple(item)
            return self.get_next_for_request(require_multimodal=require_multimodal)

    def get_next_multimodal_for_request(self, tried_keys: set[str] = None, include_cooldown: bool = True,
                                        preferred_model: str = "") -> Optional[Tuple[str, str, str, bool, int, str]]:
        with self._lock:
            if not self.key_list:
                return None
            tried_keys = tried_keys or set()
            preferred_model = str(preferred_model or "").strip()
            if preferred_model:
                for slot in self.model_slots:
                    if preferred_model not in {slot.get("model"), slot.get("display_model")}:
                        continue
                    idx = self._pick_from_slot(slot, tried_keys, include_cooldown, True)
                    if idx is not None:
                        item = self.key_list[idx]
                        self.last_selected_index = idx
                        item["last_used_at"] = self._now()
                        return self._result_tuple(item)
            for slot in self.model_slots:
                idx = self._pick_from_slot(slot, tried_keys, include_cooldown, True)
                if idx is not None:
                    item = self.key_list[idx]
                    self.last_selected_index = idx
                    item["last_used_at"] = self._now()
                    return self._result_tuple(item)
            return None

    def get_next_for_request(self, tried_keys: set[str] = None, include_cooldown: bool = True,
                             require_multimodal: bool = False) -> Optional[Tuple[str, str, str, bool, int, str]]:
        with self._lock:
            if not self.key_list or not self.model_slots:
                return None
            tried_keys = tried_keys or set()
            total = len(self.model_slots)
            start = self.current_index % total if total else 0
            for offset in range(total):
                slot_pos = (start + offset) % total
                slot = self.model_slots[slot_pos]
                idx = self._pick_from_slot(slot, tried_keys, include_cooldown, require_multimodal)
                if idx is None:
                    continue
                old = self.current_index + 1 if self.model_slots else 0
                self.current_index = slot_pos
                self.last_selected_index = idx
                item = self.key_list[idx]
                item["last_used_at"] = self._now()
                if old != slot_pos + 1:
                    self.switch_logs.append({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "from": old,
                        "to": slot_pos + 1,
                        "reason": "model rotation switch",
                        "manual": False
                    })
                return self._result_tuple(item)
            if require_multimodal:
                for offset in range(total):
                    slot_pos = (start + offset) % total
                    slot = self.model_slots[slot_pos]
                    idx = self._pick_from_slot(slot, tried_keys, include_cooldown, False)
                    if idx is None:
                        continue
                    self.current_index = slot_pos
                    self.last_selected_index = idx
                    item = self.key_list[idx]
                    item["last_used_at"] = self._now()
                    return self._result_tuple(item)
            return None

    def mark_success(self, key: str = None):
        with self._lock:
            idx = self._find_index_by_key(key)
            if idx is None:
                return
            item = self.key_list[idx]
            item["last_used_at"] = self._now()
            self.last_selected_index = idx
            self.current_index = int(item.get("rotation_index", self.current_index) or 0)

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
            old = self.current_index + 1 if self.model_slots else 0
            if self.model_slots:
                self.current_index = (int(item.get("rotation_index", self.current_index) or 0) + 1) % len(self.model_slots)
            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": old,
                "to": self.current_index + 1 if self.model_slots else 0,
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
            old = self.current_index + 1 if self.model_slots else 0
            if self.model_slots:
                self.current_index = (int(item.get("rotation_index", self.current_index) or 0) + 1) % len(self.model_slots)
            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": old,
                "to": self.current_index + 1 if self.model_slots else 0,
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
                return True
            return False

    def manual_switch_by_index(self, index: int) -> bool:
        with self._lock:
            if 1 <= index <= len(self.model_slots):
                old = self.current_index + 1 if self.model_slots else 0
                self.current_index = index - 1
                self.switch_logs.append({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": old,
                    "to": index,
                    "reason": "manual switch by rotation index",
                    "manual": True
                })
                return True
            return False

    def manual_switch_by_model(self, model: str) -> bool:
        with self._lock:
            model = str(model or "").strip()
            old = self.current_index + 1 if self.model_slots else 0
            for i, slot in enumerate(self.model_slots):
                if model in {slot.get("model"), slot.get("display_model")}:
                    self.current_index = i
                    self.switch_logs.append({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "from": old,
                        "to": i + 1,
                        "reason": f"manual switch by model: {model}",
                        "manual": True
                    })
                    return True
            return False

    def set_default_by_index(self, index: int) -> bool:
        return self.manual_switch_by_index(index)

    def set_default_by_model(self, model: str) -> bool:
        return self.manual_switch_by_model(model)

    def clear_default(self):
        with self._lock:
            self.default_index = None
            self.default_model = None

    def reset_cooldown(self, index: int) -> bool:
        with self._lock:
            if 1 <= index <= len(self.key_list):
                item = self.key_list[index - 1]
                item["cooldown_until"] = 0.0
                item["last_error"] = ""
                return True
            return False

    def is_default_key(self, key: str = None) -> bool:
        with self._lock:
            idx = self._find_index_by_key(key)
            if idx is None:
                return False
            return int(self.key_list[idx].get("rotation_index", -1)) == self.current_index

    def is_default_multimodal(self) -> bool:
        with self._lock:
            if not self.model_slots:
                return False
            slot = self.model_slots[self.current_index % len(self.model_slots)]
            return any(bool(self.key_list[idx].get("supports_multimodal", False)) for idx in slot.get("indices", []))

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
                result.append({
                    "id": i,
                    "api_name": f"{item.get('display_model') or item['model']} | {self._mask_key(item['key'])}",
                    "provider_id": item.get("provider_id", ""),
                    "base_url": item["base_url"],
                    "model": item["model"],
                    "display_model": item.get("display_model") or item["model"],
                    "key": self._mask_key(item["key"]),
                    "status": status,
                    "fail_count": item["fail_count"],
                    "last_error": item["last_error"],
                    "last_used_at": item["last_used_at"],
                    "timeout_seconds": item.get("timeout_seconds", 60),
                    "is_current": int(item.get("rotation_index", -1)) == self.current_index,
                    "is_default": int(item.get("rotation_index", -1)) == self.current_index,
                })
            return result

    def get_switch_logs(self, limit: int = 20) -> List[Dict]:
        with self._lock:
            return list(self.switch_logs)[-limit:]

    def get_current_display(self) -> str:
        with self._lock:
            if not self.model_slots:
                return "无可用 API"
            slot = self.model_slots[self.current_index % len(self.model_slots)]
            return f"{slot.get('display_model') or slot.get('model')}"

    def get_default_display(self) -> str:
        return self.get_current_display()


key_manager = SiliconFlowKeyManager()
