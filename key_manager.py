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
                self.start_index = 0
                self.last_selected_index = None
                self.key_list = []
                self.model_slots = []
                self.model_cursor = defaultdict(int)
                self.switch_logs = deque(maxlen=200)
                self._initialized = True
                self._build_key_list()
                self._ensure_slot_state()

    def set_endpoints(self, endpoints: List[Dict]):
        with self._lock:
            prev_current_identity = self._get_slot_identity(self._resolve_current_slot_pos())
            # start_index 是管理员用 /model 选定的轮询起点，必须和 current_index
            # 一样按身份还原。只还原 current_index 的话，保存一次配置就会让实际
            # 请求悄悄回到第一个模型，而界面仍显示管理员选的那个。
            prev_start_identity = self._get_slot_identity(
                self.start_index if 0 <= self.start_index < len(self.model_slots) else None
            )

            self.endpoints = endpoints or []
            self.current_index = 0
            self.start_index = 0
            self.last_selected_index = None
            self.key_list = []
            self.model_slots = []
            self.model_cursor = defaultdict(int)
            self.switch_logs.clear()
            self._build_key_list()

            restored_current = self._find_slot_pos_by_identity(prev_current_identity)
            self.current_index = restored_current if restored_current is not None else 0
            restored_start = self._find_slot_pos_by_identity(prev_start_identity)
            self.start_index = restored_start if restored_start is not None else 0
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
            try:
                seconds = int(float(default))
            except (TypeError, ValueError):
                seconds = 60
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
            model = str(endpoint.get("model", "") or "").strip()
            if not model:
                continue
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

    def _resolve_current_slot_pos(self) -> Optional[int]:
        if 0 <= self.current_index < len(self.model_slots):
            return self.current_index
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
            return
        current_slot = self._resolve_current_slot_pos()
        self.current_index = current_slot if current_slot is not None else 0

    @staticmethod
    def _attempt_identity(item: Dict) -> tuple[str, str, str]:
        """一次模型请求的唯一身份；同一 Key 可被多个渠道/模型安全复用。"""
        return (
            str(item.get("base_url", "") or ""),
            str(item.get("model", "") or ""),
            str(item.get("key", "") or ""),
        )

    def _find_index_by_key(self, key: str = None, model: str = "", base_url: str = "") -> Optional[int]:
        """定位实际请求项，优先使用最近选中的模型，避免共用 Key 时记错槽。"""
        def matches(item: Dict) -> bool:
            if key and item.get("key") != key:
                return False
            if model and item.get("model") != model:
                return False
            if base_url and item.get("base_url") != base_url:
                return False
            return True

        idx = self.last_selected_index
        if idx is not None and 0 <= idx < len(self.key_list) and matches(self.key_list[idx]):
            return idx
        if key or model or base_url:
            for i, item in enumerate(self.key_list):
                if matches(item):
                    return i
        return None

    def _is_available(self, item: Dict, include_cooldown: bool = True) -> bool:
        """模型不做冷却也不做禁用：每次请求都要允许从 Slot #1 重新开始尝试。

        include_cooldown 参数保留只为兼容旧调用签名，内部不再使用。
        """
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

    def _pick_from_slot(self, slot: Dict, tried_keys: set, include_cooldown: bool, require_multimodal: bool) -> Optional[int]:
        indices = slot.get("indices") or []
        if not indices:
            return None
        start = int(self.model_cursor.get(slot["rotation_index"], 0) or 0) % len(indices)
        for offset in range(len(indices)):
            pos = (start + offset) % len(indices)
            idx = indices[pos]
            item = self.key_list[idx]
            identity = self._attempt_identity(item)
            # 新代码使用 (base_url, model, key)；兼容外部仍传裸 key 的旧调用。
            if identity in tried_keys or item["key"] in tried_keys:
                continue
            if not self._is_available(item, include_cooldown=include_cooldown):
                continue
            if not self._matches_request(item, require_multimodal=require_multimodal):
                continue
            self.model_cursor[slot["rotation_index"]] = (pos + 1) % len(indices)
            return idx
        return None

    def _iter_slot_positions(self) -> List[int]:
        """每次请求的尝试顺序：固定从 start_index（默认 Slot #1）开始向后轮询。

        不使用 current_index 作为起点——current_index 只是"上次实际用了哪个模型"的
        展示状态。若用它做起点，一次失败切走后，后续所有请求都会从备用模型开始，
        再也回不到 Slot #1。
        """
        total = len(self.model_slots)
        if total <= 0:
            return []
        start = self.start_index if 0 <= self.start_index < total else 0
        return [(start + offset) % total for offset in range(total)]

    def _switch_current_slot(self, slot_pos: int, reason: str, manual: bool = False):
        total = len(self.model_slots)
        if total <= 0 or not (0 <= slot_pos < total):
            return
        old = self._resolve_current_slot_pos()
        self.current_index = slot_pos
        # 只有管理员手动切换才改变轮询起点；自动失败切换不改，
        # 否则下一次请求就不会再从 Slot #1 开始了。
        if manual:
            self.start_index = slot_pos
        if old is not None and old != slot_pos:
            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": self._slot_display(old) or f"模型 #{old + 1}",
                "to": self._slot_display(slot_pos) or f"模型 #{slot_pos + 1}",
                "from_index": old + 1,
                "to_index": slot_pos + 1,
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
                        self._switch_current_slot(i, f"优先使用多模态模型：{preferred_model}", manual=False)
                        return self._result_tuple(item)
            for slot_pos in self._iter_slot_positions():
                slot = self.model_slots[slot_pos]
                idx = self._pick_from_slot(slot, tried_keys, include_cooldown, True)
                if idx is not None:
                    item = self.key_list[idx]
                    self.last_selected_index = idx
                    item["last_used_at"] = self._now()
                    self._switch_current_slot(slot_pos, "按轮换顺序切换模型", manual=False)
                    return self._result_tuple(item)
            return None

    def get_preferred_for_request(self, preferred_model: str, tried_keys: set[str] = None,
                                  include_cooldown: bool = True,
                                  require_multimodal: bool = False,
                                  allow_non_multimodal_fallback: bool = True) -> Optional[Tuple[str, str, str, bool, int, str]]:
        """优先从指定模型取一个可用 Key，不改变全局默认轮换起点。"""
        preferred_model = str(preferred_model or "").strip()
        if not preferred_model:
            return self.get_next_for_request(
                tried_keys=tried_keys,
                include_cooldown=include_cooldown,
                require_multimodal=require_multimodal,
                allow_non_multimodal_fallback=allow_non_multimodal_fallback,
            )
        with self._lock:
            tried_keys = tried_keys or set()
            for slot in self.model_slots:
                if preferred_model not in {slot.get("model"), slot.get("display_model")}:
                    continue
                idx = self._pick_from_slot(slot, tried_keys, include_cooldown, require_multimodal)
                if idx is None and require_multimodal and allow_non_multimodal_fallback:
                    idx = self._pick_from_slot(slot, tried_keys, include_cooldown, False)
                if idx is not None:
                    item = self.key_list[idx]
                    item["last_used_at"] = self._now()
                    self.last_selected_index = idx
                    return self._result_tuple(item)
                break
        return self.get_next_for_request(
            tried_keys=tried_keys,
            include_cooldown=include_cooldown,
            require_multimodal=require_multimodal,
            allow_non_multimodal_fallback=allow_non_multimodal_fallback,
        )

    def get_next_for_request(self, tried_keys: set[str] = None, include_cooldown: bool = True,
                             require_multimodal: bool = False,
                             allow_non_multimodal_fallback: bool = True) -> Optional[Tuple[str, str, str, bool, int, str]]:
        with self._lock:
            if not self.key_list or not self.model_slots:
                return None
            tried_keys = tried_keys or set()
            for slot_pos in self._iter_slot_positions():
                slot = self.model_slots[slot_pos]
                idx = self._pick_from_slot(slot, tried_keys, include_cooldown, require_multimodal)
                if idx is None:
                    continue
                item = self.key_list[idx]
                item["last_used_at"] = self._now()
                self.last_selected_index = idx
                self._switch_current_slot(slot_pos, "按轮换顺序切换模型", manual=False)
                return self._result_tuple(item)
            if require_multimodal and allow_non_multimodal_fallback:
                for slot_pos in self._iter_slot_positions():
                    slot = self.model_slots[slot_pos]
                    idx = self._pick_from_slot(slot, tried_keys, include_cooldown, False)
                    if idx is None:
                        continue
                    item = self.key_list[idx]
                    item["last_used_at"] = self._now()
                    self.last_selected_index = idx
                    self._switch_current_slot(slot_pos, "按轮换顺序切换模型", manual=False)
                    return self._result_tuple(item)
            return None

    def _find_slot_pos_for_key_index(self, key_idx: Optional[int]) -> Optional[int]:
        """把 key_list 下标映射到 model_slots 下标。

        rotation_index 是原始 endpoints 下标（可能含空洞），不能直接当 current_index 用。
        """
        if key_idx is None or not (0 <= key_idx < len(self.key_list)):
            return None
        for i, slot in enumerate(self.model_slots):
            if key_idx in (slot.get("indices") or []):
                return i
        # 兜底：按 rotation_index 对齐（slot 与 key 同源时）
        rotation_index = int(self.key_list[key_idx].get("rotation_index", -1) or -1)
        for i, slot in enumerate(self.model_slots):
            if int(slot.get("rotation_index", -2) or -2) == rotation_index:
                return i
        return None

    def mark_success(self, key: str = None, model: str = "", base_url: str = ""):
        with self._lock:
            idx = self._find_index_by_key(key, model=model, base_url=base_url)
            if idx is None:
                return
            item = self.key_list[idx]
            item["last_used_at"] = self._now()
            self.last_selected_index = idx
            slot_pos = self._find_slot_pos_for_key_index(idx)
            if slot_pos is not None:
                self.current_index = slot_pos
            self._ensure_slot_state()

    def mark_failure(self, key: str = None, reason: str = "", cooldown_seconds: int = 1,
                     model: str = "", base_url: str = ""):
        """记录失败并把 current_index 指向下一个模型。

        不再写 cooldown_until：模型不做冷却，下一次请求仍要从 Slot #1 开始重试。
        cooldown_seconds 参数保留只为兼容大量既有调用点。
        """
        with self._lock:
            if not self.key_list:
                return
            idx = self._find_index_by_key(key, model=model, base_url=base_url)
            if idx is None:
                return
            item = self.key_list[idx]
            item["fail_count"] += 1
            item["last_error"] = reason
            failed_slot = self._find_slot_pos_for_key_index(idx)
            if failed_slot is None:
                failed_slot = self._resolve_current_slot_pos()
            if failed_slot is None:
                failed_slot = 0
            old = failed_slot
            if self.model_slots:
                # 失败后按轮换列表顺序切到下一个模型（仅影响本轮重试与状态展示）
                self.current_index = (failed_slot + 1) % len(self.model_slots)
            self._ensure_slot_state()
            from_name = self._slot_display(old) if old is not None else "未知模型"
            to_name = self._slot_display(self.current_index) if self.model_slots else "无可用模型"
            self.switch_logs.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "from": from_name,
                "to": to_name,
                "from_index": (old + 1) if old is not None else 0,
                "to_index": self.current_index + 1 if self.model_slots else 0,
                "reason": f"请求失败，切换下一个模型：{reason}",
                "manual": False,
            })

    def disable_key(self, key: str = None, reason: str = "", model: str = "", base_url: str = ""):
        """按用户要求：模型不做禁用，401/404 等错误一律等同普通失败。"""
        self.mark_failure(key, reason=reason, model=model, base_url=base_url)

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
                self._switch_current_slot(index - 1, "管理员按序号手动切换", manual=True)
                self._ensure_slot_state()
                return True
            return False

    def manual_switch_by_model(self, model: str) -> bool:
        with self._lock:
            model = str(model or "").strip()
            for i, slot in enumerate(self.model_slots):
                if model in {slot.get("model"), slot.get("display_model")}:
                    self._switch_current_slot(i, f"管理员手动切换到：{model}", manual=True)
                    self._ensure_slot_state()
                    return True
            return False

    def set_default_by_index(self, index: int) -> bool:
        return self.manual_switch_by_index(index)

    def set_default_by_model(self, model: str) -> bool:
        return self.manual_switch_by_model(model)

    def clear_default(self):
        pass  # ponytail: no-op, default concept removed

    def is_default_key(self, key: str = None, model: str = "", base_url: str = "") -> bool:
        # 轮换列表第一个模型（model_slots[0]）受保护：401/404 时只冷却不禁用
        with self._lock:
            idx = self._find_index_by_key(key, model=model, base_url=base_url)
            if idx is None:
                return False
            slot_pos = self._find_slot_pos_for_key_index(idx)
            return slot_pos == 0

    def is_default_multimodal(self) -> bool:
        with self._lock:
            if not self.model_slots:
                return False
            slot_pos = self._resolve_current_slot_pos()
            if slot_pos is None:
                return False
            slot = self.model_slots[slot_pos]
            return any(bool(self.key_list[idx].get("supports_multimodal", False)) for idx in slot.get("indices", []))

    def reset_cooldown(self, index: int) -> bool:
        with self._lock:
            if 1 <= index <= len(self.key_list):
                item = self.key_list[index - 1]
                item["cooldown_until"] = 0.0
                item["last_error"] = ""
                return True
            return False

    def get_key_info(self, key: str = None) -> Optional[Dict]:
        with self._lock:
            idx = self._find_index_by_key(key)
            if idx is None:
                return None
            return self.key_list[idx].copy()

    @staticmethod
    def make_attempt_identity(base_url: str, key: str, model: str) -> tuple[str, str, str]:
        return (str(base_url or ""), str(model or ""), str(key or ""))

    def get_attempt_count(self) -> int:
        """可尝试的渠道/模型/Key 组合数，而不是去重后的 Key 数。"""
        with self._lock:
            return len(self.key_list)

    def get_all_keys(self) -> List[str]:
        with self._lock:
            return [item["key"] for item in self.key_list]

    def get_status_list(self) -> List[Dict]:
        with self._lock:
            now = self._now()
            current_slot = self._resolve_current_slot_pos()
            start_slot = self.start_index if 0 <= self.start_index < len(self.model_slots) else 0
            result = []
            for i, item in enumerate(self.key_list, start=1):
                if item["disabled"]:
                    status = "disabled"
                elif item["cooldown_until"] > now:
                    left = int(item["cooldown_until"] - now)
                    status = f"cooldown({left}s)"
                else:
                    status = "active"
                # rotation_index 是原始 endpoints 下标，可能含空洞；必须映射到 model_slots 下标
                slot_pos = self._find_slot_pos_for_key_index(i - 1)
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
                    "supports_multimodal": bool(item.get("supports_multimodal", False)),
                    "slot_index": (slot_pos + 1) if slot_pos is not None else None,
                    "is_current": slot_pos is not None and slot_pos == current_slot,
                    # is_default 表示「每次请求从这里开始轮询」，也就是管理员用
                    # /model 选定的起点；不是固定的槽位 1。失败切换只改 is_current，
                    # 界面上两者要能分开看。
                    "is_default": slot_pos is not None and slot_pos == start_slot,
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
        """轮询起点的显示名。

        不能直接返回 get_current_display()：current 会被失败切换改掉，
        而起点只有管理员手动切换才变。两者混为一谈的话，界面显示的「默认模型」
        会在一次失败后跟着漂移，管理员看不出自己选的到底是哪个。
        """
        with self._lock:
            if not self.model_slots:
                return "无可用 API"
            pos = self.start_index if 0 <= self.start_index < len(self.model_slots) else 0
            return self._slot_display(pos)


key_manager = SiliconFlowKeyManager()
