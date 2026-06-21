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
                if self.model_slots:
                    self.default_index = 0
                    self.default_model = self._slot_display(0)
                self._ensure_slot_state()

    def set_endpoints(self, endpoints: List[Dict]):
        with self._lock:
            had_previous_slots = bool(self.model_slots)
            prev_default_identity = self._get_slot_identity(self._resolve_default_slot_pos())
            prev_current_identity = self._get_slot_identity(self._resolve_current_slot_pos())

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

            restored_default = self._find_slot_pos_by_identity(prev_default_identity)
            restored_current = self._find_slot_pos_by_identity(prev_current_identity)
            if restored_default is None and self.model_slots and not had_previous_slots:
                restored_default = 0
            self.default_index = restored_default
            self.default_model = self._slot_display(restored_default) if restored_default is not None else None
            self.current_index = restored_current if restored_current is not None else (restored_default if restored_default is not None else 0)
            self._ensure_slot_state()

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

    def _resolve_default_slot_pos(self) -> Optional[int]:
        if self.default_index is None:
            return None
        if 0 <= self.default_index < len(self.model_slots):
            return self.default_index
        return None

    def _resolve_current_slot_pos(self) -> Optional[int]:
        if 0 <= self.current_index < len(self.model_slots):
            return self.current_index
        default_slot = self._resolve_default_slot_pos()
        if default_slot is not None:
            return default_slot
        if self.model_slots:
            return 0
        return None

    def _slot_display(self, slot_pos: Optional[int]) -> str:
        if slot_pos is None or not (0 <= slot_pos < len(self.model_slots)):
            return ""
        slot = self.model_slots[slot_pos]
        return str(slot.get("display_model") or slot.get("model") or "")

    def _get_slot_identity(self, slot_pos: Optional[int]) -> Optional[tuple[str, str, str]]:
        if slot_pos is None or not (0 <= slot_pos < len(self.model_slots)):
            return None
        slot = self.model_slots[slot_pos]
        return (
            str(slot.get("provider_id", "") or "").strip(),
            str(slot.get("model", "") or "").strip(),
            str(slot.get("display_model", "") or "").strip(),
        )

    def _find_slot_pos_by_identity(self, identity: Optional[tuple[str, str, str]]) -> Optional[int]:
        if identity is None:
            return None
        for i, slot in enumerate(self.model_slots):
            slot_identity = (
                str(slot.get("provider_id", "") or "").strip(),
                str(slot.get("model", "") or "").strip(),
                str(slot.get("display_model", "") or "").strip(),
            )
            if slot_identity == identity:
                return i
        return None

    def _ensure_slot_state(self):
        if not self.model_slots:
            self.current_index = 0
            self.default_index = None
            self.default_model = None
            return
        if self.default_index is not None and not (0 <= self.default_index < len(self.model_slots)):
            self.default_index = None
            self.default_model = None
        current_slot = self._resolve_current_slot_pos()
        self.current_index = current_slot if current_slot is not None else 0
        default_slot = self._resolve_default_slot_pos()
        self.default_model = self._slot_display(default_slot) if default_slot is not None else None

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

    def _iter_slot_positions(self, prefer_default: bool = True) -> List[int]:
        total = len(self.model_slots)
        if total <= 0:
            return []
        current_slot = self._resolve_current_slot_pos()
        default_slot = self._resolve_default_slot_pos() if prefer_default else None
        start = current_slot if current_slot is not None else (default_slot if default_slot is not None else 0)
        order: list[int] = []
        seen = set()

        def add(slot_pos: Optional[int]):
            if slot_pos is None:
                return
            if not (0 <= slot_pos < total):
                return
            if slot_pos in seen:
                return
            seen.add(slot_pos)
            order.append(slot_pos)

        add(default_slot)
        add(current_slot)
        for offset in range(total):
            add((start + offset) % total)
        return order

    def _switch_current_slot(self, slot_pos: int, reason: str, manual: bool = False):
        total = len(self.model_slots)
        if total <= 0 or not (0 <= slot_pos < total):
            return
        old = self._resolve_current_slot_pos()
        self.current_index = slot_pos
        if old is not None and old != slot_pos:
            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": old + 1,
                "to": slot_pos + 1,
                "reason": reason,
                "manual": manual,
            })

    def get_current(self, require_multimodal: bool = False) -> Optional[Tuple[str, str, str, bool, int, str]]:
        with self._lock:
            if not self.key_list or not self.model_slots:
                return None
            slot_pos = self._resolve_current_slot_pos()
            if slot_pos is None:
                return None
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
            if not self.key_list or not self.model_slots:
                return None
            tried_keys = tried_keys or set()
            preferred_model = str(preferred_model or "").strip()
            if preferred_model:
                for i, slot in enumerate(self.model_slots):
                    if preferred_model not in {slot.get("model"), slot.get("display_model")}:
                        continue
                    idx = self._pick_from_slot(slot, tried_keys, include_cooldown, True)
                    if idx is not None:
                        item = self.key_list[idx]
                        self.last_selected_index = idx
                        item["last_used_at"] = self._now()
                        self._switch_current_slot(i, f"prefer multimodal model: {preferred_model}", manual=False)
                        return self._result_tuple(item)
            for slot_pos in self._iter_slot_positions(prefer_default=True):
                slot = self.model_slots[slot_pos]
                idx = self._pick_from_slot(slot, tried_keys, include_cooldown, True)
                if idx is not None:
                    item = self.key_list[idx]
                    self.last_selected_index = idx
                    item["last_used_at"] = self._now()
                    reason = "return to default" if slot_pos == self._resolve_default_slot_pos() else "model rotation switch"
                    self._switch_current_slot(slot_pos, reason, manual=False)
                    return self._result_tuple(item)
            return None

    def get_next_for_request(self, tried_keys: set[str] = None, include_cooldown: bool = True,
                             require_multimodal: bool = False) -> Optional[Tuple[str, str, str, bool, int, str]]:
        with self._lock:
            if not self.key_list or not self.model_slots:
                return None
            tried_keys = tried_keys or set()
            default_slot = self._resolve_default_slot_pos()
            for slot_pos in self._iter_slot_positions(prefer_default=True):
                slot = self.model_slots[slot_pos]
                idx = self._pick_from_slot(slot, tried_keys, include_cooldown, require_multimodal)
                if idx is None:
                    continue
                item = self.key_list[idx]
                item["last_used_at"] = self._now()
                self.last_selected_index = idx
                reason = "return to default" if default_slot is not None and slot_pos == default_slot else "model rotation switch"
                self._switch_current_slot(slot_pos, reason, manual=False)
                return self._result_tuple(item)
            if require_multimodal:
                for slot_pos in self._iter_slot_positions(prefer_default=True):
                    slot = self.model_slots[slot_pos]
                    idx = self._pick_from_slot(slot, tried_keys, include_cooldown, False)
                    if idx is None:
                        continue
                    item = self.key_list[idx]
                    item["last_used_at"] = self._now()
                    self.last_selected_index = idx
                    reason = "return to default" if default_slot is not None and slot_pos == default_slot else "model rotation switch"
                    self._switch_current_slot(slot_pos, reason, manual=False)
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
            self._ensure_slot_state()

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
            failed_slot = int(item.get("rotation_index", self.current_index) or 0)
            old = self._resolve_current_slot_pos()
            if self.model_slots:
                self.current_index = (failed_slot + 1) % len(self.model_slots)
            self._ensure_slot_state()
            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": (old + 1) if old is not None else 0,
                "to": self.current_index + 1 if self.model_slots else 0,
                "reason": f"failure: {reason}, cooldown={cooldown_seconds}s",
                "manual": False,
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
            failed_slot = int(item.get("rotation_index", self.current_index) or 0)
            old = self._resolve_current_slot_pos()
            if self.model_slots:
                self.current_index = (failed_slot + 1) % len(self.model_slots)
            self._ensure_slot_state()
            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": (old + 1) if old is not None else 0,
                "to": self.current_index + 1 if self.model_slots else 0,
                "reason": f"disabled: {reason}",
                "manual": False,
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
                self._switch_current_slot(index - 1, "manual switch by rotation index", manual=True)
                self._ensure_slot_state()
                return True
            return False

    def manual_switch_by_model(self, model: str) -> bool:
        with self._lock:
            model = str(model or "").strip()
            for i, slot in enumerate(self.model_slots):
                if model in {slot.get("model"), slot.get("display_model")}:
                    self._switch_current_slot(i, f"manual switch by model: {model}", manual=True)
                    self._ensure_slot_state()
                    return True
            return False

    def set_default_by_index(self, index: int) -> bool:
        with self._lock:
            if not (1 <= index <= len(self.model_slots)):
                return False
            self.default_index = index - 1
            self.default_model = self._slot_display(self.default_index)
            self._switch_current_slot(self.default_index, "manual set default by rotation index", manual=True)
            self._ensure_slot_state()
            return True

    def set_default_by_model(self, model: str) -> bool:
        with self._lock:
            model = str(model or "").strip()
            for i, slot in enumerate(self.model_slots):
                if model in {slot.get("model"), slot.get("display_model")}:
                    self.default_index = i
                    self.default_model = self._slot_display(i)
                    self._switch_current_slot(i, f"manual set default by model: {model}", manual=True)
                    self._ensure_slot_state()
                    return True
            return False

    def clear_default(self):
        with self._lock:
            self.default_index = None
            self.default_model = None
            self._ensure_slot_state()

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
            default_slot = self._resolve_default_slot_pos()
            if default_slot is None:
                default_slot = self._resolve_current_slot_pos()
            if default_slot is None:
                return False
            return int(self.key_list[idx].get("rotation_index", -1)) == default_slot

    def is_default_multimodal(self) -> bool:
        with self._lock:
            if not self.model_slots:
                return False
            slot_pos = self._resolve_default_slot_pos()
            if slot_pos is None:
                slot_pos = self._resolve_current_slot_pos()
            if slot_pos is None:
                return False
            slot = self.model_slots[slot_pos]
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
            current_slot = self._resolve_current_slot_pos()
            default_slot = self._resolve_default_slot_pos()
            result = []
            for i, item in enumerate(self.key_list, start=1):
                if item["disabled"]:
                    status = "disabled"
                elif item["cooldown_until"] > now:
                    left = int(item["cooldown_until"] - now)
                    status = f"cooldown({left}s)"
                else:
                    status = "active"
                slot_pos = int(item.get("rotation_index", -1))
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
                    "is_current": slot_pos == current_slot,
                    "is_default": default_slot is not None and slot_pos == default_slot,
                })
            return result

    def get_switch_logs(self, limit: int = 20) -> List[Dict]:
        with self._lock:
            return list(self.switch_logs)[-limit:]

    def get_current_display(self) -> str:
        with self._lock:
            slot_pos = self._resolve_current_slot_pos()
            if slot_pos is None:
                return "无可用 API"
            return self._slot_display(slot_pos)

    def get_default_display(self) -> str:
        with self._lock:
            slot_pos = self._resolve_default_slot_pos()
            if slot_pos is None:
                return "未设置"
            return self._slot_display(slot_pos)


key_manager = SiliconFlowKeyManager()
