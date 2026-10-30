# -*- coding: utf-8 -*-
"""主动回复串行调度器；planner/实际发送由 main 注入。

用户活动只更新内存，tick/退出批量落盘；每条成功主动消息立即落盘。
main 应在 planner 开始时取得 activity_version，并在每条发送前调用 can_send。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from bot.io_json import atomic_write_json
from bot.paths import BASE_DIR

DEFAULT_OBSERVATION_CONFIG: dict[str, Any] = {
    "enabled": True,
    "trigger_probability": 0.5,
    "default_wait_seconds": 60,
    "min_wait_seconds": 10,
    "max_wait_seconds": 600,
    "debounce_seconds": 8,
    "max_debounce_seconds": 20,
    "min_continuation_interval_seconds": 30,
}
_OBSERVATION_BOUNDS = {
    "trigger_probability": (0, 1),
    "default_wait_seconds": (1, 3600),
    "min_wait_seconds": (1, 3600),
    "max_wait_seconds": (1, 3600),
    "debounce_seconds": (0, 300),
    "max_debounce_seconds": (0, 300),
    "min_continuation_interval_seconds": (1, 3600),
}


def _normalize_observation(raw: Any, kind: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{kind}.observation must be an object")
    cfg = {**copy.deepcopy(DEFAULT_OBSERVATION_CONFIG), **copy.deepcopy(raw)}
    # 已废弃的总窗口/聊天轮数硬停不再参与调度，其余扩展字段仍保留。
    cfg.pop("window_seconds", None)
    cfg.pop("max_continuation_rounds", None)
    if type(cfg["enabled"]) is not bool:
        raise ValueError(f"{kind}.observation.enabled must be bool")
    for name, (minimum, maximum) in _OBSERVATION_BOUNDS.items():
        value = cfg[name]
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"{kind}.observation.{name} must be in [{minimum}, {maximum}]")
    if not cfg["min_wait_seconds"] <= cfg["default_wait_seconds"] <= cfg["max_wait_seconds"]:
        raise ValueError(f"{kind}.observation wait bounds must contain default_wait_seconds")
    if cfg["max_debounce_seconds"] < cfg["debounce_seconds"]:
        raise ValueError(f"{kind}.observation.max_debounce_seconds must be >= debounce_seconds")
    return cfg


DEFAULT_GROUP_CONFIG: dict[str, Any] = {
    "enabled": False,
    "trigger_probability": 0.3,
    "check_interval_seconds": 60,
    "idle_threshold_seconds": 900,
    "max_consecutive_bot_messages": 3,
    "min_interval_between_proactive_seconds": 1800,
    "whitelist": [],
    "blacklist": [],
    "observation": copy.deepcopy(DEFAULT_OBSERVATION_CONFIG),
}
DEFAULT_PRIVATE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "trigger_probability": 0.1,
    "check_interval_seconds": 120,
    "idle_threshold_seconds": 43200,
    "max_unanswered_messages": 3,
    "min_interval_between_proactive_seconds": 86400,
    "whitelist": [],
    "blacklist": [],
    "observation": copy.deepcopy(DEFAULT_OBSERVATION_CONFIG),
}


def normalize_config(raw: Any) -> dict[str, dict[str, Any]]:
    """合并 group/private 默认值；非法值抛 ValueError，绝不把坏名单当作全开。

    enabled 只接受 bool；数值不接受 bool/字符串/非有限数；概率为 [0, 1]，
    冷场检查间隔至少 5 秒，其余冷场时间非负，熔断次数为正整数。
    observation 单次等待 1~3600 秒，debounce 0~300 秒，续聊间隔 1~3600 秒；
    min/default/max 等待和 debounce/max_debounce 同时校验顺序。
    名单只接受正整数 QQ 号或对应十进制字符串组成的 list。
    """
    if not isinstance(raw, dict):
        raise ValueError("ProactiveReply must be an object")
    result = {}
    for kind, defaults in (("group", DEFAULT_GROUP_CONFIG), ("private", DEFAULT_PRIVATE_CONFIG)):
        section = raw.get(kind, {})
        if not isinstance(section, dict):
            raise ValueError(f"{kind} must be an object")
        cfg = {**copy.deepcopy(defaults), **copy.deepcopy(section)}
        cfg["observation"] = _normalize_observation(cfg["observation"], kind)
        if type(cfg["enabled"]) is not bool:
            raise ValueError(f"{kind}.enabled must be bool")
        for name in defaults:
            value = cfg[name]
            if name in ("enabled", "whitelist", "blacklist", "observation"):
                continue
            if type(value) not in (int, float):
                raise ValueError(f"{kind}.{name} must be a finite number")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{kind}.{name} must be finite")
            minimum = 5 if name == "check_interval_seconds" else 0
            if value < minimum:
                raise ValueError(f"{kind}.{name} must be >= {minimum}")
            if name == "trigger_probability" and value > 1:
                raise ValueError(f"{kind}.{name} must be in [0, 1]")
            if name.startswith("max_") and (type(value) is not int or value < 1):
                raise ValueError(f"{kind}.{name} must be a positive integer")
        for name in ("whitelist", "blacklist"):
            value = cfg[name]
            if not isinstance(value, list):
                raise ValueError(f"{kind}.{name} must be a list")
            ids = []
            for item in value:
                if type(item) not in (int, str):
                    raise ValueError(f"{kind}.{name} contains an invalid QQ id")
                text = str(item).strip()
                if not text or not text.isascii() or not text.isdecimal() or not text.strip("0"):
                    raise ValueError(f"{kind}.{name} contains an invalid QQ id")
                ids.append(text.lstrip("0"))
            cfg[name] = list(dict.fromkeys(ids))
        result[kind] = cfg
    return result


_STATE_DIR = os.path.join(str(BASE_DIR), "data", "proactive_state")
MAX_SESSIONS = 1000
RECENT_PROACTIVE_LIMIT = 10
RECENT_CONTENT_LIMIT = 1000
RECENT_INCOMING_LIMIT = 128


@dataclass(eq=False)
class _ReplyObservationTicket:
    key: tuple[str, str]
    activity_version: int
    lifecycle: object
    digest: bytes
    completed: bool = False


@dataclass
class _ObservationWake:
    generation: int
    activity_version: int
    new_messages: bool
    timeout: bool
    # 同一次逻辑发送的拆段可以跳过刚更新的续聊间隔，但不能跳过熔断。
    sent: bool = False
    initial_wait: bool = False


@dataclass
class _Observation:
    generation: int
    started_at: float
    wait_until: float | None
    pending_since: float | None = None
    message_due_at: float | None = None
    message_sequence: int = 0
    consumed_sequence: int = 0
    wake_count: int = 0
    last_wake_at: float | None = None
    wake: _ObservationWake | None = None
    rearm_intent: tuple[int, float | None, bool] | None = None
    initial_wait_pending: bool = False


def _state_path(kind: str, chat_id: str) -> str:
    safe_id = "".join(c for c in str(chat_id) if c.isalnum() or c in ("_", "-"))
    return os.path.join(_STATE_DIR, f"{kind}_{safe_id}.json")


def _default_state() -> dict[str, Any]:
    return {
        "last_proactive_at": 0.0,
        "last_user_message_at": 0.0,
        "unanswered_count": 0,
        "consecutive_bot_count": 0,
        "total_proactive_sent": 0,
        "activity_version": 0,
        # 每条成功主动消息：{"sent_at": float, "content": str}，最多 10 条。
        "recent_proactive_messages": [],
    }


class _StateStore:
    """锁内 read/modify/write；flush 用独立写锁和 revision 防止写回覆盖。

    缓存只淘汰已落盘项。满且全脏时拒绝新 update，下一次 flush 后可重试；
    这样用户入口无需为淘汰而 fsync，也不会静默丢弃未持久化状态。
    """

    def __init__(self, max_entries: int = MAX_SESSIONS) -> None:
        self._lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._dirty: dict[tuple[str, str], int] = {}
        self._revision = 0
        self._max_entries = max_entries

    def _read(self, kind: str, chat_id: str) -> dict[str, Any]:
        state = _default_state()
        try:
            with open(_state_path(kind, chat_id), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for name, default in state.items():
                    value = data.get(name, default)
                    if name == "recent_proactive_messages":
                        if isinstance(value, list):
                            state[name] = [
                                {"sent_at": item["sent_at"], "content": item["content"][:RECENT_CONTENT_LIMIT]}
                                for item in value[-RECENT_PROACTIVE_LIMIT:]
                                if isinstance(item, dict) and isinstance(item.get("content"), str)
                                and type(item.get("sent_at")) in (int, float)
                                and math.isfinite(item["sent_at"])
                            ]
                    elif type(value) in (int, float) and math.isfinite(value) and value >= 0:
                        if type(default) is not int or type(value) is int:
                            state[name] = value
        except (OSError, ValueError, TypeError, OverflowError):
            pass
        return state

    def _make_room(self) -> bool:
        if len(self._cache) < self._max_entries:
            return True
        for key in self._cache:
            if key not in self._dirty:
                del self._cache[key]
                return True
        return False

    def _load_locked(self, key: tuple[str, str]) -> dict[str, Any]:
        if key in self._cache:
            state = self._cache.pop(key)
            self._cache[key] = state  # LRU；无 TTL，支持七天以上的空闲阈值。
            return state
        state = self._read(*key)
        if self._make_room():
            self._cache[key] = state
        return state

    def load(self, kind: str, chat_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load_locked((kind, str(chat_id))))

    def update(self, kind: str, chat_id: str,
               mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
        """原子更新并标脏；容量全脏时返回 None，不进行磁盘写入。"""
        key = (kind, str(chat_id))
        with self._lock:
            state = self._load_locked(key)
            if key not in self._cache:
                return None
            updated = copy.deepcopy(state)
            mutate(updated)
            self._cache[key] = updated
            self._revision += 1
            self._dirty[key] = self._revision
            return copy.deepcopy(updated)

    def flush(self, kind: str | None = None, chat_id: str | None = None) -> None:
        """持久化脏快照；写失败保持 dirty，由下次 tick/finally 重试。"""
        with self._flush_lock:
            with self._lock:
                pending = [
                    (key, revision, copy.deepcopy(self._cache[key]))
                    for key, revision in self._dirty.items()
                    if kind is None or key == (kind, str(chat_id))
                ]
            errors = []
            for key, revision, state in pending:
                try:
                    atomic_write_json(_state_path(*key), state, indent=2)
                except Exception as exc:
                    errors.append(exc)
                    continue
                with self._lock:
                    if self._dirty.get(key) == revision:
                        del self._dirty[key]
            if errors:
                raise OSError(f"proactive state flush failed: {errors[0]}") from errors[0]

    def is_fused(self, key: tuple[str, str], cfg: dict[str, Any]) -> bool:
        with self._lock:
            state = self._cache.get(key, {})
            counter, limit = _counter_limit(key[0], cfg)
            return state.get(counter, 0) >= limit


def _counter_limit(kind: str, cfg: dict[str, Any]) -> tuple[str, int]:
    if kind == "group":
        return "consecutive_bot_count", cfg["max_consecutive_bot_messages"]
    return "unanswered_count", cfg["max_unanswered_messages"]


class ProactiveReplyScheduler:
    TICK_SECONDS = 30.0
    MAX_ACTIVE_SESSIONS = MAX_SESSIONS

    def __init__(
        self,
        get_config: Callable[[], dict[str, Any]],
        run_group_planner: Callable[[str, dict[str, Any]], Awaitable[bool]],
        run_private_planner: Callable[[str, dict[str, Any]], Awaitable[bool]],
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._get_config = get_config
        self._run_group = run_group_planner
        self._run_private = run_private_planner
        self._log = log or (lambda msg: None)
        self._store = _StateStore()
        self._lock = threading.RLock()
        self._active_sessions: dict[tuple[str, str], float] = {}
        self._last_check_at: dict[tuple[str, str], float] = {}
        self._observations: dict[tuple[str, str], _Observation] = {}
        self._begin_eligible: dict[tuple[str, str], int] = {}
        self._recent_incoming: dict[tuple[str, str], dict[bytes, bool | None]] = {}
        self._observation_generation = 0
        self._reply_tickets: dict[tuple[str, str], _ReplyObservationTicket] = {}
        self._lifecycle = object()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def _report(self, message: str) -> None:
        if message.startswith("observation "):
            if message.startswith("observation cancel ") and "reason=incoming" in message:
                return
            fields = dict(re.findall(r"(\w+)=([^\s]+)", message))
            event = message.split()[1]
            labels = {"begin": "进入持续观察", "cancel": "取消观察任务", "end": "观察任务结束",
                      "reject": "本次未建立回复后的观察", "skip": "本次不继续观察",
                      "rearm": "调整观察等待", "wake": "观察唤醒", "finish": "结束观察请求"}
            reasons = {"incoming": "收到新消息", "normal_route": "常规回复接管", "policy": "当前策略不允许",
                       "no_wait": "本轮未继续预约等待", "finish": "模型请求结束", "stale_caller": "原请求已失效",
                       "stale_ticket": "回复资格已失效", "not_final_success": "未确认完整回复（未发送、部分发送或观察登记未完成）",
                       "probability_miss": "未命中观察概率",
                       "disabled": "主动回复未开启", "observation_disabled": "持续观察未开启",
                       "not_allowed": "会话不在允许范围", "stopped": "调度器已停止", "inactive": "会话未激活",
                       "fused": "达到防打扰限制", "invalid_or_stale": "等待参数或观察资格无效",
                       "initial_wait": "首次自主等待规划", "default_fallback": "回退默认等待",
                       "model_choice": "模型自主选择", "wait": "预约等待"}
            parts = [labels.get(event, "观察状态更新")]
            if "session" in fields:
                session = fields["session"].replace("private_", "私聊 ", 1).replace("group_", "群聊 ", 1)
                parts.append("会话：" + session)
            names = {"source": "触发来源", "version": "活动版本", "generation": "观察代次", "wait": "等待时长", "reason": "原因", "success": "执行结果", "probability": "观察概率"}
            values = {"normal": "普通回复", "proactive": "主动发言", "planner": "规划器自主等待", "true": "成功", "false": "未执行"}
            for key, value in fields.items():
                if key == "session":
                    continue
                shown = reasons.get(value, values.get(value, value))
                parts.append(f"{names.get(key, key)}：{shown}{' 秒' if key == 'wait' else ''}")
            message = "｜".join(parts)
        try:
            self._log(f"[ProactiveReply] {message}")
        except Exception:
            pass

    def _config(self) -> dict[str, dict[str, Any]]:
        try:
            config = normalize_config(self._get_config())
        except Exception as exc:
            self._report("配置无效，关闭闸门 reason=invalid_config")
            config = normalize_config({})
        for kind, section in config.items():
            if not section["enabled"]:
                self.cancel_observations("disabled", kind=kind)
        return config

    def _flush(self) -> None:
        try:
            self._store.flush()
        except Exception as exc:
            self._report("state flush failed reason=storage_error")

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="proactive-reply", daemon=True)
            self._thread.start()
        self._report("调度器已启动")

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop.set()
            self.cancel_observations(reason="stop")
            thread, loop, task = self._thread, self._event_loop, self._task
            if loop is not None and task is not None:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:  # loop 恰好已经退出。
                    pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            alive = bool(thread and thread.is_alive())
            if not alive and self._thread is thread:
                self._thread = None
        if not alive:
            self._flush()  # 也支持未 start 的实例。
        self._report("停止超时，保留存活线程引用" if alive else "调度器已停止")

    @staticmethod
    def _is_allowed(cfg: dict[str, Any], chat_id: str) -> bool:
        cid = str(chat_id)
        return cid not in cfg["blacklist"] and (not cfg["whitelist"] or cid in cfg["whitelist"])

    def _register(self, kind: str, chat_id: str, cfg_all: dict[str, dict[str, Any]]) -> None:
        key = (kind, chat_id)
        with self._lock:
            if self._stop.is_set():
                return
            if key not in self._active_sessions and len(self._active_sessions) >= self.MAX_ACTIVE_SESSIONS:
                # 优先移除已经熔断且最久不活跃者；不删除会话状态文件。
                victim = min(self._active_sessions, key=lambda k: (
                    not self._store.is_fused(k, cfg_all[k[0]]), self._active_sessions[k]))
                del self._active_sessions[victim]
                self._last_check_at.pop(victim, None)
                self._observations.pop(victim, None)
                self._begin_eligible.pop(victim, None)
                self._recent_incoming.pop(victim, None)
                self._reply_tickets.pop(victim, None)
            self._active_sessions[key] = time.time()

    def touch_session(self, is_group: bool, chat_id: str) -> None:
        kind = "group" if is_group else "private"
        with self._lock:
            cfg_all = self._config()
            if cfg_all[kind]["enabled"] and self._is_allowed(cfg_all[kind], str(chat_id)):
                self._register(kind, str(chat_id), cfg_all)

    def _cancel_episode_locked(self, key: tuple[str, str], reason: str = "policy") -> None:
        self._begin_eligible.pop(key, None)
        if reason != "normal_route":
            self._reply_tickets.pop(key, None)
        episode = self._observations.pop(key, None)
        if episode is not None:
            self._report(f"observation {'end' if reason in ('finish', 'no_wait') else 'cancel'} session={key[0]}_{key[1]} generation={episode.generation} reason={reason}")
            # 普通路由的入站已递增版本；不再次使该消息自己的回复资格失效。
            if reason != "normal_route" and self._store.update(*key, lambda state: state.update(
                    activity_version=state["activity_version"] + 1)) is None:
                self._active_sessions.pop(key, None)
                self._last_check_at.pop(key, None)
                self._recent_incoming.pop(key, None)

    def _observation_locked(self, key: tuple[str, str], cfg: dict[str, Any]) -> _Observation | None:
        if (self._stop.is_set() or key not in self._active_sessions or not cfg["enabled"]
                or not cfg["observation"]["enabled"] or not self._is_allowed(cfg, key[1])):
            self._cancel_episode_locked(key)
            return None
        return self._observations.get(key)

    def notify_user_message_received(self, is_group: bool, chat_id: str, *, message_id: str = "") -> _ReplyObservationTicket | None:
        """活动登记同锁签发一次性普通回复观察资格；重复事件不重新签发。"""
        return self._note_incoming(is_group, chat_id, normal_handled=None, message_id=message_id)

    def complete_normal_reply(self, is_group: bool, chat_id: str, *, normal_handled: bool,
                              message_id: str = "", ticket: object = None,
                              reply_sent: bool = False) -> bool:
        """原子确认入站路由，再消费生成前取得的资格；不得发送后冒领新版本。"""
        if type(normal_handled) is not bool:
            raise ValueError("normal_handled must be bool")
        key = ("group" if is_group else "private", str(chat_id))
        with self._lock:
            # 必须先捕获新鲜度：晚到的 route confirmation 不能替旧回复认领新活动。
            valid = (isinstance(ticket, _ReplyObservationTicket) and ticket.key == key
                     and ticket.lifecycle is self._lifecycle
                     and self._reply_tickets.get(key) is ticket
                     and ticket.activity_version == self._store.load(*key)["activity_version"]
                     and ticket.digest == hashlib.sha256(message_id.encode("utf-8")).digest())
            digest = hashlib.sha256(message_id.encode("utf-8")).digest() if message_id else None
            # reset/disconnect 已清掉路由记录时，迟到的 finally 不是新的用户活动。
            if (not isinstance(ticket, _ReplyObservationTicket)
                    or digest in self._recent_incoming.get(key, {})):
                self.note_incoming(is_group, chat_id, normal_handled=normal_handled, message_id=message_id)
            if not isinstance(ticket, _ReplyObservationTicket) or ticket.completed:
                return False
            ticket.completed = True
            if self._reply_tickets.get(key) is ticket:
                self._reply_tickets.pop(key, None)
            if not normal_handled:
                return False
            cfg = self._config()[key[0]]
            state = self._store.load(*key)
            reason = ("stale_ticket" if not valid or state["activity_version"] != ticket.activity_version else
                      "not_final_success" if not reply_sent or not normal_handled else
                      "stopped" if self._stop.is_set() else
                      "disabled" if not cfg["enabled"] else
                      "not_allowed" if not self._is_allowed(cfg, key[1]) else
                      "observation_disabled" if not cfg["observation"]["enabled"] else
                      "inactive" if key not in self._active_sessions else
                      "fused" if not self._eligible(*key, cfg, state, time.time(), continuation=True) else "")
            if reason:
                self._report(f"observation reject session={key[0]}_{key[1]} source=normal version={ticket.activity_version} reason={reason}")
                return False
            seconds = float(cfg["observation"]["default_wait_seconds"])
            return self._start_observation_locked(key, seconds, "normal", state["activity_version"])

    def _start_observation_locked(self, key, seconds, source, version):
        cfg = self._config()[key[0]]
        if (self._stop.is_set() or not cfg["enabled"] or not cfg["observation"]["enabled"]
                or key not in self._active_sessions or not self._is_allowed(cfg, key[1])):
            return False
        if source in ("normal", "proactive"):
            probability = cfg["observation"]["trigger_probability"]
            if probability <= 0 or random.random() >= probability:
                self._report(f"observation skip session={key[0]}_{key[1]} source={source} version={version} reason=probability_miss probability={probability:g}")
                return False
        self._observation_generation += 1
        now = time.time()
        episode = _Observation(self._observation_generation, now, now + seconds,
                               initial_wait_pending=source == "normal")
        self._observations[key] = episode
        self._report(f"observation begin session={key[0]}_{key[1]} source={source} version={version} generation={episode.generation} wait={seconds:g}")
        return True

    def note_incoming(self, is_group: bool, chat_id: str, *,
                      normal_handled: bool, message_id: str = "") -> None:
        """路由确定后调用；普通 AI/命令（即使被 weak 拒绝）优先于观察。"""
        if type(normal_handled) is not bool:
            raise ValueError("normal_handled must be bool")
        self._note_incoming(is_group, chat_id, normal_handled=normal_handled, message_id=message_id)

    def _note_incoming(self, is_group: bool, chat_id: str, *,
                       normal_handled: bool | None, message_id: str) -> _ReplyObservationTicket | None:
        kind, chat_id = ("group" if is_group else "private"), str(chat_id)
        key = (kind, chat_id)
        with self._lock:
            cfg_all = self._config()
            cfg = cfg_all[kind]
            # 与 stop/热关闭的标记排序，入口不落盘。
            if self._stop.is_set():
                return
            if not cfg["enabled"] or not self._is_allowed(cfg, chat_id):
                self._cancel_episode_locked(key)
                return
            digest = hashlib.sha256(message_id.encode("utf-8")).digest() if message_id else None
            recent = self._recent_incoming.get(key, {})
            if digest is not None and digest in recent:
                # notify 先失效旧结果，note 后确认路由；同一 ID 只记一次活动/pending。
                previous = recent[digest]
                if normal_handled is True and previous is not True:
                    recent[digest] = True
                    if kind == "group":
                        self._cancel_episode_locked(key, reason="normal_route")
                elif normal_handled is False and previous is None:
                    recent[digest] = False
                    if kind == "group":
                        self._schedule_message_locked(key, cfg, time.time())
                return
            self._begin_eligible.pop(key, None)
            self._reply_tickets.pop(key, None)
            now = time.time()
            def mutate(state: dict[str, Any]) -> None:
                state["last_user_message_at"] = now
                state["unanswered_count"] = 0
                state["consecutive_bot_count"] = 0
                state["activity_version"] += 1
            updated = self._store.update(kind, chat_id, mutate)
            if updated is None:
                # 容量全脏时撤销资格，不能让旧 planner 继续发送。
                self._active_sessions.pop(key, None)
                self._last_check_at.pop(key, None)
                self._observations.pop(key, None)
                self._recent_incoming.pop(key, None)
                return
            self._register(kind, chat_id, cfg_all)
            if digest is not None:
                recent = self._recent_incoming.setdefault(key, {})
                recent[digest] = normal_handled
                if len(recent) > RECENT_INCOMING_LIMIT:
                    del recent[next(iter(recent))]
            ticket = None
            if normal_handled is None and digest is not None:
                ticket = _ReplyObservationTicket(key, updated["activity_version"], self._lifecycle, digest)
                self._reply_tickets[key] = ticket
            if normal_handled is True or kind == "private":
                # 活动版本已经递增；私聊在入站即让位，不等待正常生成完成。
                episode = self._observations.pop(key, None)
                if episode is not None:
                    self._report(f"observation cancel session={kind}_{chat_id} generation={episode.generation} reason=incoming")
                return ticket
            if normal_handled is False:
                self._schedule_message_locked(key, cfg, now)
            else:
                self._observation_locked(key, cfg)
            return ticket

    def _schedule_message_locked(self, key: tuple[str, str], cfg: dict[str, Any], now: float) -> None:
        episode = self._observation_locked(key, cfg)
        if episode is None:
            return
        episode.message_sequence += 1
        if episode.pending_since is None:
            episode.pending_since = now
        observation = cfg["observation"]
        episode.message_due_at = min(now + observation["debounce_seconds"],
            episode.pending_since + observation["max_debounce_seconds"])

    @staticmethod
    def _wait_seconds(seconds: Any, cfg: dict[str, Any]) -> float | None:
        try:
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                return None
        except OverflowError:
            return None
        return float(max(cfg["min_wait_seconds"], min(seconds, cfg["max_wait_seconds"])))

    def begin_observation(self, is_group: bool, chat_id: str, *,
                          wait_seconds: float | None = None) -> None:
        """首次主动逻辑发送成功后显式调用；续聊/拆段不能重复 begin。"""
        kind, chat_id = ("group" if is_group else "private"), str(chat_id)
        cfg_all = self._config()
        cfg = cfg_all[kind]
        observation = cfg["observation"]
        seconds = self._wait_seconds(observation["default_wait_seconds"]
            if wait_seconds is None else wait_seconds, observation)
        key = (kind, chat_id)
        with self._lock:
            if (self._stop.is_set() or not cfg["enabled"] or not observation["enabled"]
                    or not self._is_allowed(cfg, chat_id) or seconds is None):
                self._cancel_episode_locked(key)
                self._report(f"observation reject session={kind}_{chat_id} source=proactive reason=policy_or_wait")
                return
            state = self._store.load(kind, chat_id)
            if key in self._observations:
                return
            # 只接受本进程成功发送的资格；重启、finish、用户新活动均不能复活旧消息。
            if (self._begin_eligible.pop(key, None) != state["activity_version"]
                    or not self._eligible(kind, chat_id, cfg, state, time.time(), continuation=True)):
                self._report(f"observation reject session={kind}_{chat_id} source=proactive version={state['activity_version']} reason=stale_or_fused")
                return
            self._register(kind, chat_id, cfg_all)
            self._start_observation_locked(key, seconds, "proactive", state["activity_version"])

    def _matches_observation_locked(self, key, episode, expected_activity_version, expected_generation):
        """无期望参数保持旧工具语义；绑定的冷场请求（generation=None）不能操作任何新 episode。"""
        if (expected_activity_version is not None
                and self._store.load(*key)["activity_version"] != expected_activity_version):
            return False
        if expected_generation is not None:
            return episode is not None and episode.generation == expected_generation
        return expected_activity_version is None or episode is None

    def set_observation_wait(self, is_group: bool, chat_id: str, seconds: float, *,
                             expected_activity_version: int | None = None,
                             expected_generation: int | None = None,
                             allow_start: bool = False, planner_choice: bool = False) -> bool:
        """预约等待；规划器无效或超过默认时长的结果使用默认值兜底。"""
        key = ("group" if is_group else "private", str(chat_id))
        cfg = self._config()[key[0]]
        fallback = False
        if planner_choice:
            observation = cfg["observation"]
            try:
                valid_seconds = (type(seconds) in (int, float) and math.isfinite(seconds)
                                 and observation["min_wait_seconds"] <= seconds <= observation["default_wait_seconds"])
            except OverflowError:
                valid_seconds = False
            fallback = not valid_seconds
            if fallback:
                seconds = observation["default_wait_seconds"]
        seconds = self._wait_seconds(seconds, cfg["observation"])
        with self._lock:
            # 先验原资格，再做策略处理；旧调用不得修改新 episode 的任何字段。
            if not self._matches_observation_locked(key, self._observations.get(key),
                    expected_activity_version, expected_generation):
                self._report(f"observation rearm session={key[0]}_{key[1]} reason=stale_caller success=false")
                return False
            episode = self._observation_locked(key, cfg)
            if (episode is None and allow_start and seconds is not None
                    and expected_activity_version is not None and expected_generation is None
                    and key in self._active_sessions and cfg['observation']['enabled']
                    and not any(v is None for v in self._recent_incoming.get(key, {}).values())):
                state = self._store.load(*key)
                if self._eligible(*key, cfg, state, time.time()):
                    self._start_observation_locked(key, seconds, "planner", state['activity_version'])
                    return True
            valid = (episode is not None and seconds is not None and (episode.wake is None
                     or self._store.load(*key)["activity_version"] == episode.wake.activity_version))
            intent = (episode.generation if episode is not None else 0, seconds, valid)
            if episode is not None and episode.wake is not None and episode.rearm_intent == intent:
                return valid
            if episode is not None:
                episode.rearm_intent = intent
            if not valid:
                self._report(f"observation rearm session={key[0]}_{key[1]} reason=invalid_or_stale success=false")
                return False
            initial_wait = episode.wake is not None and episode.wake.initial_wait
            episode.wait_until = (episode.started_at if initial_wait else time.time()) + seconds
            self._report(f"observation rearm session={key[0]}_{key[1]} generation={episode.generation} wait={seconds:g} reason={'default_fallback' if fallback else 'model_choice' if planner_choice else 'wait'} success=true")
            return True

    def finish_observation(self, is_group: bool, chat_id: str, *,
                           expected_activity_version: int | None = None,
                           expected_generation: int | None = None) -> bool:
        key = ("group" if is_group else "private", str(chat_id))
        with self._lock:
            if not self._matches_observation_locked(key, self._observations.get(key),
                    expected_activity_version, expected_generation):
                self._report(f"observation finish session={key[0]}_{key[1]} reason=stale_caller success=false")
                return False
            self._cancel_episode_locked(key, reason="finish")
            return True

    def cancel_observations(self, reason: str = "disconnect", *, kind: str | None = None) -> None:
        """撤销全部或指定类型资格；保留防打扰计数，重开不恢复旧任务。"""
        if kind not in (None, "group", "private"):
            raise ValueError("invalid conversation kind")
        with self._lock:
            observation_count = sum(kind is None or key[0] == kind for key in self._observations)
            if kind is None:
                self._lifecycle = object()
            maps = (self._reply_tickets, self._observations, self._begin_eligible,
                    self._active_sessions, self._last_check_at, self._recent_incoming)
            keys = {key for mapping in maps for key in mapping if kind is None or key[0] == kind}
            for key in keys:
                self._store.update(*key, lambda state: state.update(
                    activity_version=state["activity_version"] + 1))
                for mapping in maps:
                    mapping.pop(key, None)
        if observation_count:
            reason_text = reason if reason in ("disconnect", "stop", "restart") else "cancelled"
            self._report(f"已取消 {observation_count} 个观察任务：{reason_text}")

    def notify_bot_message_sent(self, is_group: bool, chat_id: str, *,
                                is_proactive: bool, content: str = "") -> bool:
        """每条成功发送后调用一次；被动 bot 回复不冒充真实用户清零。

        群聊每条 bot 消息计入连发；私聊仅主动消息计入未回复。
        已成功的主动发送即使刚被关闭/stop 也要记账并立即持久化。
        """
        kind, chat_id = ("group" if is_group else "private"), str(chat_id)
        if not is_proactive and (self._stop.is_set() or not self._config()[kind]["enabled"]):
            return False
        def mutate(state: dict[str, Any]) -> None:
            state["consecutive_bot_count"] += 1
            if is_proactive:
                now = time.time()
                state["last_proactive_at"] = now
                state["total_proactive_sent"] += 1
                if kind == "private":
                    state["unanswered_count"] += 1
                state["recent_proactive_messages"] = (state["recent_proactive_messages"] + [
                    {"sent_at": now, "content": str(content)[:RECENT_CONTENT_LIMIT]}
                ])[-RECENT_PROACTIVE_LIMIT:]
        with self._lock:
            if not is_proactive and self._stop.is_set():
                return False
            state = self._store.update(kind, chat_id, mutate)
            episode = self._observations.get((kind, chat_id))
            if (is_proactive and state is not None and episode is not None and episode.wake is not None
                    and episode.wake.activity_version == state["activity_version"]):
                episode.wake.sent = True
        if state is None and is_proactive:
            # 成功发送不能丢账：仅发送回调允许在容量满时先 flush 后重试。
            self._store.flush()
            state = self._store.update(kind, chat_id, mutate)
        if is_proactive:
            if state is None:
                raise RuntimeError("proactive state capacity exhausted")
            self._store.flush(kind, chat_id)
            with self._lock:
                key = (kind, chat_id)
                if (not self._stop.is_set() and key in self._active_sessions
                        and key not in self._observations
                        and self._store.load(kind, chat_id)["activity_version"] == state["activity_version"]):
                    self._begin_eligible[key] = state["activity_version"]
        return state is not None

    def reset_session(self, is_group: bool, chat_id: str) -> None:
        """清空会话主动上下文并立即落盘，保留递增版本使旧生成失效。

        不受 enabled/stop 限制，不将 reset 注册为用户活动。写失败向调用方
        抛出异常，脏状态仍保留供下次 flush 重试；不删除状态文件。
        """
        kind, chat_id = ("group" if is_group else "private"), str(chat_id)
        def mutate(state: dict[str, Any]) -> None:
            version = state["activity_version"] + 1
            state.update(_default_state())
            state["activity_version"] = version
        with self._lock:
            self._active_sessions.pop((kind, chat_id), None)
            self._last_check_at.pop((kind, chat_id), None)
            episode = self._observations.pop((kind, chat_id), None)
            if episode is not None:
                self._report(f"observation cancel session={kind}_{chat_id} generation={episode.generation} reason=reset")
            self._begin_eligible.pop((kind, chat_id), None)
            self._recent_incoming.pop((kind, chat_id), None)
            self._reply_tickets.pop((kind, chat_id), None)
            state = self._store.update(kind, chat_id, mutate)
            if state is None:
                # reset 是显式持久化操作，允许先刷出全脏缓存再接纳目标状态。
                self._store.flush()
                state = self._store.update(kind, chat_id, mutate)
            if state is None:
                raise RuntimeError("proactive state capacity exhausted during reset")
            self._store.flush(kind, chat_id)

    def get_session_state(self, is_group: bool, chat_id: str) -> dict[str, Any]:
        """返回隔离副本；观察只公开有限摘要，不持久化/恢复等待任务。"""
        key = ("group" if is_group else "private", str(chat_id))
        cfg = self._config()[key[0]]
        with self._lock:
            episode = self._observation_locked(key, cfg)
            state = self._store.load(*key)
            state["observation"] = None if episode is None else {
                "generation": episode.generation,
                "started_at": episode.started_at,
                "wait_until": episode.wait_until,
                "message_due_at": episode.message_due_at,
                "new_messages": episode.message_sequence > episode.consumed_sequence,
                "wake_count": episode.wake_count,
                "in_flight": episode.wake is not None,
            }
            return state

    def _eligible(self, kind: str, chat_id: str, cfg: dict[str, Any],
                  state: dict[str, Any], now: float, continuation: bool = False) -> bool:
        if self._stop.is_set() or not cfg["enabled"] or not self._is_allowed(cfg, chat_id):
            return False
        counter, limit = _counter_limit(kind, cfg)
        if state[counter] >= limit:
            return False
        if continuation:
            return True
        last_user, last_sent = state["last_user_message_at"], state["last_proactive_at"]
        return (last_user > 0 and now - last_user >= cfg["idle_threshold_seconds"]
                and (last_sent <= 0 or now - last_sent >= cfg["min_interval_between_proactive_seconds"]))

    def can_send(self, is_group: bool, chat_id: str, activity_version: int,
                 continuation: bool = False, *, observation_generation: int | None = None) -> bool:
        """逐条提交闸门；observation 与逻辑发送拆段 continuation 相互独立。"""
        kind, chat_id = ("group" if is_group else "private"), str(chat_id)
        key = (kind, chat_id)
        cfg = self._config()[kind]
        with self._lock:
            episode = self._observation_locked(key, cfg)
            if (key not in self._active_sessions or any(
                    value is None for value in self._recent_incoming.get(key, {}).values())):
                return False
            state = self._store.load(kind, chat_id)
            if type(activity_version) is not int or state["activity_version"] != activity_version:
                return False
            now = time.time()
            if observation_generation is None:
                return self._eligible(kind, chat_id, cfg, state, now, continuation)
            wake = episode.wake if episode is not None else None
            if (type(observation_generation) is not int or wake is None
                    or wake.generation != observation_generation
                    or wake.activity_version != activity_version
                    or not (wake.initial_wait or wake.new_messages or wake.timeout)):
                return False
            if not self._eligible(kind, chat_id, cfg, state, now, continuation=True):
                return False
            if wake.initial_wait:
                return True
            return (continuation and wake.sent) or (
                now - state["last_proactive_at"] >= cfg["observation"]["min_continuation_interval_seconds"])

    def _prepare_observation(self, key: tuple[str, str], cfg: dict[str, Any],
                             now: float) -> _ObservationWake | None:
        """串行 tick 取走一个 pending；判断未发送也不会重复消费相同消息/timeout。"""
        with self._lock:
            episode = self._observation_locked(key, cfg)
            if episode is None or episode.wake is not None:
                return None
            state = self._store.load(*key)
            if not self._eligible(*key, cfg, state, now, continuation=True):
                self._cancel_episode_locked(key)
                return None
            # 路由未定时不得抢答，notify 已令任何旧 runner 的 version 失效。
            if any(value is None for value in self._recent_incoming.get(key, {}).values()):
                return None
            if episode.initial_wait_pending:
                episode.initial_wait_pending = False
                self._observation_generation += 1
                episode.generation = self._observation_generation
                wake = _ObservationWake(episode.generation, state["activity_version"], False, False,
                                        initial_wait=True)
                episode.wake = wake
                self._report(f"observation wake session={key[0]}_{key[1]} version={wake.activity_version} generation={wake.generation} reason=initial_wait")
                return wake
            new_messages = episode.message_sequence > episode.consumed_sequence
            timeout = episode.wait_until is not None and now >= episode.wait_until
            due = (new_messages and episode.message_due_at is not None
                   and now >= episode.message_due_at)
            if (new_messages and not due) or (not due and not timeout):
                return None
            interval = cfg["observation"]["min_continuation_interval_seconds"]
            # 最短 wake 间隔限制 judge 成本；无固定聊天总轮数/总窗口。
            if (now - state["last_proactive_at"] < interval
                    or (episode.last_wake_at is not None and now - episode.last_wake_at < interval)):
                return None
            self._observation_generation += 1
            episode.generation = self._observation_generation
            wake = _ObservationWake(episode.generation, state["activity_version"], new_messages, timeout)
            episode.wake = wake
            episode.wake_count += 1
            episode.last_wake_at = now
            episode.consumed_sequence = episode.message_sequence
            episode.pending_since = None
            episode.message_due_at = None
            # 一次 wake 消费旧等待。必须通过新的 wait intent 才能再 timeout。
            episode.wait_until = None
            self._report(f"observation wake session={key[0]}_{key[1]} version={wake.activity_version} generation={wake.generation} reason={'messages' if new_messages else 'timeout'}")
            return wake

    def _complete_observation(self, key: tuple[str, str], wake: _ObservationWake) -> None:
        cfg = self._config()[key[0]]
        with self._lock:
            episode = self._observation_locked(key, cfg)
            if episode is None or episode.wake is not wake:
                return
            episode.wake = None
            if (episode.wait_until is None
                    and episode.message_sequence == episode.consumed_sequence
                    and not any(value is None for value in self._recent_incoming.get(key, {}).values())):
                self._cancel_episode_locked(key, reason="no_wait")

    def _loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._event_loop = loop
            self._task = loop.create_task(self._async_loop())
            task = self._task
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._report("调度线程退出 reason=scheduler_error")
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for pending_task in pending:
                    pending_task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                self.cancel_observations(reason="restart")
                self._flush()
                loop.close()
                asyncio.set_event_loop(None)
                with self._lock:
                    self._event_loop = None
                    self._task = None

    async def _async_loop(self) -> None:
        next_cold_tick = time.monotonic() + self.TICK_SECONDS
        try:
            while not self._stop.is_set():
                # 一条串行循环，无逐会话 task/线程；观察最多约 1 秒检测延迟。
                await self._wait_or_stop(min(1.0, max(0.0, next_cold_tick - time.monotonic())))
                if not self._stop.is_set():
                    cold_due = time.monotonic() >= next_cold_tick
                    if cold_due:
                        next_cold_tick = time.monotonic() + self.TICK_SECONDS
                    await self._tick_async(include_cold=cold_due)
        finally:
            self._flush()

    async def _wait_or_stop(self, seconds: float) -> None:
        # stop() 通过 call_soon_threadsafe(task.cancel) 唤醒，无 executor/辅助线程。
        if not self._stop.is_set():
            await asyncio.sleep(seconds)

    async def _tick_async(self, *, include_cold: bool = True) -> None:
        try:
            with self._lock:
                sessions = list(self._active_sessions)
            observed = set()
            # 先扫描 observation，再冷场。两者都只使用同一串行 runner。
            for kind, chat_id in sessions:
                if self._stop.is_set():
                    break
                key = (kind, chat_id)
                wake = None
                try:
                    cfg = self._config()[kind]
                    with self._lock:
                        if key in self._observations:
                            observed.add(key)
                    wake = self._prepare_observation(key, cfg, time.time())
                    if wake is not None:
                        cfg["_observation"] = {
                            "generation": wake.generation,
                            "new_messages": wake.new_messages,
                            "timeout": wake.timeout,
                            "initial_wait": wake.initial_wait,
                        }
                        runner = self._run_group if kind == "group" else self._run_private
                        await runner(chat_id, cfg)
                except Exception as exc:
                    self._report(f"planner failure session={kind}_{chat_id} reason=observation_error")
                finally:
                    if wake is not None:
                        self._complete_observation(key, wake)
            if include_cold:
                for kind, chat_id in sessions:
                    if self._stop.is_set():
                        break
                    if (kind, chat_id) in observed:
                        continue
                    try:
                        # 前一 planner 可能很慢，逐会话读取最新配置、时刻和状态。
                        cfg = self._config()[kind]
                        if self._should_trigger(kind, chat_id, cfg, time.time()):
                            self._report(f"触发 {kind} {chat_id}")
                            runner = self._run_group if kind == "group" else self._run_private
                            await runner(chat_id, cfg)
                    except Exception as exc:
                        self._report(f"planner failure session={kind}_{chat_id} reason=cold_error")
        finally:
            self._flush()

    def _should_trigger(self, kind: str, chat_id: str, cfg: dict[str, Any], now: float) -> bool:
        if self._stop.is_set() or not cfg["enabled"]:
            return False
        key = (kind, chat_id)
        with self._lock:
            # 扫描快照中的会话可能已被容量淘汰，不能重新引入 last_check。
            if (key not in self._active_sessions or key in self._observations or any(
                    value is None for value in self._recent_incoming.get(key, {}).values())):
                return False
            last_check = self._last_check_at.get(key)
            if last_check is not None and now - last_check < cfg["check_interval_seconds"]:
                return False
            self._last_check_at[key] = now
        state = self._store.load(kind, chat_id)
        if not self._eligible(kind, chat_id, cfg, state, now):
            return False
        probability = cfg["trigger_probability"]
        return probability > 0 and random.random() < probability


_scheduler: ProactiveReplyScheduler | None = None
_scheduler_lock = threading.Lock()


def start_proactive_scheduler(
    get_config: Callable[[], dict[str, Any]],
    run_group_planner: Callable[[str, dict[str, Any]], Awaitable[bool]],
    run_private_planner: Callable[[str, dict[str, Any]], Awaitable[bool]],
    log: Callable[[str], None] | None = None,
) -> ProactiveReplyScheduler:
    """幂等启动；停止超时的旧实例仍占有单例，不允许重叠调度。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            thread = _scheduler._thread
            if not _scheduler._stop.is_set() or (thread and thread.is_alive()):
                return _scheduler
        sched = ProactiveReplyScheduler(get_config, run_group_planner, run_private_planner, log)
        _scheduler = sched
        sched.start()
        return sched


def stop_proactive_scheduler() -> None:
    global _scheduler
    with _scheduler_lock:
        sched = _scheduler
    if sched is not None:
        sched.stop()
        with _scheduler_lock:
            if _scheduler is sched and not (sched._thread and sched._thread.is_alive()):
                _scheduler = None


def reset_session(is_group: bool, chat_id: str) -> None:
    """重置当前调度器的会话状态；未启动调度器时与其他通知钩子一致无操作。"""
    sched = _scheduler
    if sched is not None:
        sched.reset_session(is_group, chat_id)


def get_session_state(is_group: bool, chat_id: str) -> dict[str, Any]:
    sched = _scheduler
    return sched.get_session_state(is_group, chat_id) if sched is not None else {
        **_default_state(), "observation": None}


def can_send(is_group: bool, chat_id: str, activity_version: int,
             continuation: bool = False, *, observation_generation: int | None = None) -> bool:
    sched = _scheduler
    return sched is not None and sched.can_send(is_group, chat_id, activity_version,
        continuation, observation_generation=observation_generation)


def notify_user_message_received(is_group: bool, chat_id: str, *, message_id: str = "") -> _ReplyObservationTicket | None:
    sched = _scheduler
    return sched.notify_user_message_received(is_group, chat_id, message_id=message_id) if sched is not None else None


def complete_normal_reply(is_group: bool, chat_id: str, *, normal_handled: bool,
                          message_id: str = "", ticket: object = None, reply_sent: bool = False) -> bool:
    sched = _scheduler
    return sched is not None and sched.complete_normal_reply(is_group, chat_id,
        normal_handled=normal_handled, message_id=message_id, ticket=ticket, reply_sent=reply_sent)


def note_incoming(is_group: bool, chat_id: str, *,
                  normal_handled: bool, message_id: str = "") -> None:
    sched = _scheduler
    if sched is not None:
        sched.note_incoming(is_group, chat_id, normal_handled=normal_handled, message_id=message_id)


def begin_observation(is_group: bool, chat_id: str, *, wait_seconds: float | None = None) -> None:
    sched = _scheduler
    if sched is not None:
        sched.begin_observation(is_group, chat_id, wait_seconds=wait_seconds)


def set_observation_wait(is_group: bool, chat_id: str, seconds: float, *,
                         expected_activity_version: int | None = None,
                         expected_generation: int | None = None,
                         allow_start: bool = False, planner_choice: bool = False) -> bool:
    sched = _scheduler
    return sched is not None and sched.set_observation_wait(is_group, chat_id, seconds,
        expected_activity_version=expected_activity_version, expected_generation=expected_generation,
        allow_start=allow_start, planner_choice=planner_choice)


def finish_observation(is_group: bool, chat_id: str, *,
                       expected_activity_version: int | None = None,
                       expected_generation: int | None = None) -> bool:
    sched = _scheduler
    return sched is not None and sched.finish_observation(is_group, chat_id,
        expected_activity_version=expected_activity_version, expected_generation=expected_generation)


def cancel_observations(reason: str = "disconnect", *, kind: str | None = None) -> None:
    sched = _scheduler
    if sched is not None:
        sched.cancel_observations(reason, kind=kind)


def notify_bot_message_sent(is_group: bool, chat_id: str, *,
                            is_proactive: bool, content: str = "") -> bool:
    sched = _scheduler
    return sched is not None and sched.notify_bot_message_sent(is_group, chat_id, is_proactive=is_proactive, content=content)


def touch_session(is_group: bool, chat_id: str) -> None:
    sched = _scheduler
    if sched is not None:
        sched.touch_session(is_group, chat_id)
