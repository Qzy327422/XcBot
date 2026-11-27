# -*- coding: utf-8 -*-
"""角色生活：配置归一化、虚拟时钟状态机、独立调度线程、单 worker 作业队列与只读快照。

设计约束：
- 拥有独立的 `RoleLifeScheduler` 后台线程定期调用 `advance(now)`，不依赖主动回复调度器。
- 计时、到点、睡眠判断全部本地完成；只有真正需要文字时才排队调用模型。
- 队列有界、去重、按角色公平轮转；同一时刻最多一个受管 worker 任务。
- 每个实际请求前先扣角色额度、再扣共享账号额度；生成器不调用 `reserve_attempt`
  就一定没有发出请求。
- 所有落盘经 `RoleLifeStore`（进程内锁 + 原子写），网络等待不持状态锁。
- 迟到结果在提交时重新校验账号代次、配置 revision、数据 epoch 与启用状态。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import threading
import time as _time
from collections import deque
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from bot.persona import Persona, persona_role_key
from bot.role_life_store import (
    GLOBAL_ACCOUNT,
    GLOBAL_ROLE,
    MAX_DELIVERY_ITEMS,
    MAX_DELIVERY_TICKETS,
    MAX_DELIVERY_TARGETS,
    MAX_OP_REQUESTS,
    RoleLifeConflict,
    RoleLifeCorruptError,
    RoleLifeStore,
    empty_state,
    migrate_legacy_global_state,
    prune_runtime_state,
    validate_account,
)

try:  # 时区数据缺失时给出明确错误，不静默回退到本地时区。
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment]

JOB_KINDS = ("plan", "activity", "diary", "dream", "maintenance")
JOB_FINISHED = ("succeeded", "failed", "unknown")

MAX_ROLE_QUEUE = 32
MAX_TOTAL_QUEUE = 128
MAX_JOB_HISTORY = 190  # job_order 上限；淘汰已终结记录，绝不删 pending/running
MAX_DEDUPE_KEYS_STATE = 2000
MAX_PLAN_ITEMS = 12
DEFAULT_ACCOUNT_DAILY_LIMIT = 192
# 离线判定：超过该间隔没有本进程/持久化推进记录，视为断档，过期活动保守处理。
ADVANCE_GAP_SECONDS = 900
# 稳态下 last_advance 检查点的有限周期落盘（不随每个 tick 写）。
ADVANCE_CHECKPOINT_SECONDS = 300

DEFAULT_ROLE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "plan_enabled": True,
    "activity_enabled": True,
    "sleep_enabled": True,
    "diary_enabled": True,
    "dream_enabled": True,
    "mood_tone_enabled": True,
    "env_awareness_enabled": True,
    "maintenance_enabled": False,
    "maintenance_interval_seconds": 1800,
    "timezone": "Asia/Shanghai",
    "sleep_start": "00:30",
    "sleep_hours": 8,
    "diary_time": "23:50",
    "activities_per_day": 5,
    "retention_days": 90,
    "model_override": "",
    "preference": "",
}

_BOOL_KEYS = (
    "enabled", "plan_enabled", "activity_enabled", "sleep_enabled",
    "diary_enabled", "dream_enabled", "mood_tone_enabled",
    "env_awareness_enabled", "maintenance_enabled",
)
_INT_BOUNDS = {
    "maintenance_interval_seconds": (300, 86400),
    "sleep_hours": (1, 12),
    "activities_per_day": (1, MAX_PLAN_ITEMS),
    "retention_days": (1, 365),
}
_TIME_KEYS = ("sleep_start", "diary_time")


def _parse_hhmm(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 5:
        raise ValueError(f"{name} must be HH:MM")
    try:
        hour, minute = text.split(":")
        hour, minute = int(hour), int(minute)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{name} must be HH:MM")
    return f"{hour:02d}:{minute:02d}"


def normalize_role_config(raw: Any) -> dict[str, Any]:
    """白名单 + 边界；未知键丢弃，非法值抛 ValueError。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("RoleLife role config must be an object")
    cfg = copy.deepcopy(DEFAULT_ROLE_CONFIG)
    for key, value in raw.items():
        if key in cfg:
            cfg[key] = copy.deepcopy(value)
    for key in _BOOL_KEYS:
        if type(cfg[key]) is not bool:
            raise ValueError(f"RoleLife.{key} must be bool")
    for key, (minimum, maximum) in _INT_BOUNDS.items():
        value = cfg[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"RoleLife.{key} must be an integer in [{minimum}, {maximum}]")
    for key in _TIME_KEYS:
        cfg[key] = _parse_hhmm(cfg[key], f"RoleLife.{key}")
    tz_name = str(cfg["timezone"] or "").strip()
    if not tz_name or len(tz_name) > 64:
        raise ValueError("RoleLife.timezone must be a valid IANA name")
    if ZoneInfo is None:
        raise ValueError("timezone support unavailable: install tzdata")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError("RoleLife.timezone must be a valid IANA name") from None
    cfg["timezone"] = tz_name
    model = cfg["model_override"]
    if not isinstance(model, str) or len(model) > 120:
        raise ValueError("RoleLife.model_override must be a string up to 120 chars")
    cfg["model_override"] = model.strip()
    preference = cfg["preference"]
    if not isinstance(preference, str) or len(preference) > 500:
        raise ValueError("RoleLife.preference must be a string up to 500 chars")
    cfg["preference"] = preference.strip()
    return cfg


def resolve_global_config(section: Any) -> dict[str, Any]:
    """解析统一全局角色生活配置。

    支持两种结构：
    - 新版扁平配置：{"enabled": true, "sleep_start": "00:30", ...}
    - 旧版嵌套配置：{"roles": {"123456": {"p1": {...}}}}（自动优先取已启用项迁移）
    """
    if not isinstance(section, Mapping) or not section:
        return copy.deepcopy(DEFAULT_ROLE_CONFIG)
    if any(k in DEFAULT_ROLE_CONFIG for k in section):
        return normalize_role_config(section)
    roles = section.get("roles")
    if isinstance(roles, Mapping):
        fallback = None
        for _acct, by_role in roles.items():
            if not isinstance(by_role, Mapping):
                continue
            for _rid, cfg in by_role.items():
                if not isinstance(cfg, Mapping):
                    continue
                try:
                    norm = normalize_role_config(cfg)
                except ValueError:
                    continue
                if norm.get("enabled"):
                    return norm
                if fallback is None:
                    fallback = norm
        if fallback is not None:
            return fallback
    return copy.deepcopy(DEFAULT_ROLE_CONFIG)


def normalize_role_life_section(raw: Any) -> dict[str, Any]:
    """整个 RoleLife 配置段。

    - 新版统一配置：扁平字段（直接作为唯一全局角色生活配置）。
    - 旧版多角色配置：仅含 {"roles": {account: {role_id: cfg}}} 时保持兼容归一化。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("RoleLife must be an object")
    if "roles" in raw and not any(k in DEFAULT_ROLE_CONFIG for k in raw):
        roles_raw = raw.get("roles", {})
        if roles_raw is None:
            roles_raw = {}
        if not isinstance(roles_raw, dict):
            raise ValueError("RoleLife.roles must be an object")
        roles: dict[str, dict[str, dict[str, Any]]] = {}
        for account, by_role in roles_raw.items():
            if not isinstance(by_role, dict):
                raise ValueError("RoleLife.roles entries must be objects")
            account = validate_account(account)
            clean_roles: dict[str, dict[str, Any]] = {}
            for role_id, cfg in by_role.items():
                role_id = str(role_id or "").strip()
                if not role_id or len(role_id) > 64:
                    raise ValueError("RoleLife role id must be 1..64 chars")
                clean_roles[role_id] = normalize_role_config(cfg)
            roles[account] = clean_roles
        return {"roles": roles}
    return normalize_role_config(raw)


def role_config(section: Any, account: str, role_id: str) -> dict[str, Any] | None:
    if str(account or "") == GLOBAL_ACCOUNT and str(role_id or "") == GLOBAL_ROLE:
        return resolve_global_config(section)
    if not isinstance(section, Mapping):
        return None
    roles = section.get("roles")
    if not isinstance(roles, Mapping):
        return None
    by_role = roles.get(str(account))
    if not isinstance(by_role, Mapping):
        return None
    cfg = by_role.get(str(role_id))
    return copy.deepcopy(cfg) if isinstance(cfg, Mapping) else None


def config_revision(cfg: Mapping[str, Any] | None) -> str:
    """角色生活配置 revision：配置变化使在途旧回包失效。

    与 persona revision 相互独立：改作息只应作废生活任务，改人设只应使
    模型请求中的角色模板过期，两者分别校验。
    """
    payload = json.dumps(dict(cfg or {}), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 时间工具：全部使用角色配置的时区，跨午夜由“本地日期 + HH:MM”显式计算。
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, tz_name: str) -> None:
        self.tz = ZoneInfo(tz_name)

    def local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), self.tz)

    def date_str(self, ts: float) -> str:
        return self.local(ts).date().isoformat()

    def hhmm(self, day: str, text: str) -> float:
        hour, minute = (int(x) for x in text.split(":"))
        y, m, d = (int(x) for x in day.split("-"))
        return datetime(y, m, d, hour, minute, tzinfo=self.tz).timestamp()

    def day_start(self, day: str) -> float:
        return self.hhmm(day, "00:00")

    def plus_hours(self, ts: float, hours: float) -> float:
        """加“真实经过的小时”。日界仍由本地日历的 day_start/next_day_start 决定。"""
        return ts + hours * 3600

    def next_day_start(self, day: str) -> float:
        return self.hhmm(self.add_days(day, 1), "00:00")

    def add_days(self, day: str, delta: int) -> str:
        y, m, d = (int(x) for x in day.split("-"))
        return (_date(y, m, d) + timedelta(days=delta)).isoformat()


@dataclass
class LifeJob:
    """传给模型适配器的只读作业快照；适配器不读写状态文件。

    `persona_template` 是**未渲染**的人设模板（`{user_name}` 仍是占位符），
    共享生活生成必须用中性称呼自行 `render`，绝不携带任何会话/私聊材料。
    `revision` 是角色人设 revision，`config_revision` 是本角色生活配置的
    独立 revision；提交时两者都会重新校验。
    """

    job_id: str
    account: str
    role_id: str
    role_key: str
    kind: str
    revision: str
    config_revision: str
    epoch: int
    generation: int
    persona_template: str
    config: dict[str, Any]
    args: dict[str, Any]


class LifeNotSent(RuntimeError):
    """适配器确定没有发出请求（额度/存储/未启用）；failed 且不重试。"""


class LifeBudgetDenied(LifeNotSent):
    """reserve_attempt 拒绝；作业 failed，未发出请求。"""


class LifeRequestRejected(RuntimeError):
    """服务商明确拒绝且未计费/无结果；允许有限一次重试。"""


class LifeGenerationUnknown(RuntimeError):
    """请求可能已发出/计费但结果不可知；作业记为 unknown，绝不自动重放。"""


class _StaleJob(Exception):
    """锁内发现作业状态已变化；中止且不发请求。"""


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class RoleLifeEngine:
    def __init__(
        self,
        store: RoleLifeStore,
        *,
        config_getter: Callable[[], Mapping[str, Any]],
        persona_getter: Callable[[], Mapping[str, Any]],
        bot_name: str | Callable[[], str] = "",
        generate: Callable[[LifeJob, Callable[[], Awaitable[bool]]], Awaitable[Any]],
        reserve_account: Callable[[str], bool] | None = None,
        account_limit: Callable[[], int] | None = None,
        clock: Callable[[], float] = _time.time,
        log: Callable[[str], None] | None = None,
    ) -> None:
        """
        参数契约：
        - `config_getter()` → 已归一化的 RoleLife 段 `{"roles": {account: {role_id: cfg}}}`。
        - `persona_getter()` → 运行时 `Others` 映射（含 personality_presets 与
          各 active 键），供本模块用 `bot.persona.resolve_persona` 自行解析；
          绝不向 main 索取聊天上下文或历史。
        - `bot_name` → 字符串或返回字符串的函数；只用于解析角色身份。
        - `generate(job, reserve_attempt)` → 异步；每次真实请求前 `await
          reserve_attempt()`，拒绝时抛 LifeBudgetDenied；按异常类别标记结果。
        """
        self.store = store
        self._config_getter = config_getter
        self._persona_getter = persona_getter
        self._bot_name = bot_name
        self._generate = generate
        self._reserve_account = reserve_account or (lambda _account: True)
        self._account_limit = account_limit
        self._clock = clock
        self._log = log or (lambda _msg: None)
        self._account: str | None = None
        self._generation: int | None = None
        self._lifecycle_lock = threading.RLock()
        self._stopping = threading.Event()
        self._queue_lock = threading.Lock()
        self._pending: dict[tuple[str, str], deque[str]] = {}
        self._queued_ids: set[tuple[str, str, str]] = set()
        self._active_ids: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._queue_size = 0
        # 真正的轮转队列：出队后把仍有作业的角色移到队尾，A,B,A,B 交替。
        self._role_rotation: deque[tuple[str, str]] = deque()
        self._rotation_set: set[tuple[str, str]] = set()
        # 本进程内每个角色的最近推进时刻，用于区分“连续运行”与“离线回来”。
        self._advance_times: dict[tuple[str, str], float] = {}

    # -- 生命周期 ---------------------------------------------------------
    def bind_account(self, account_id: str, generation: int, *,
                     is_current: Callable[[], bool] | None = None) -> bool:
        account = validate_account(account_id)
        with self._lifecycle_lock:
            if self._stopping.is_set():
                return False
            if is_current is not None and not is_current():
                return False
            self._account = account
            self._generation = int(generation)
            return True

    def unbind_account(self, generation: int | None = None, *,
                       is_current: Callable[[], bool] | None = None) -> None:
        with self._lifecycle_lock:
            if is_current is not None and not is_current():
                return
            if generation is not None and self._generation != generation:
                return
            self._account = None
            self._generation = None
        with self._queue_lock:
            self._pending.clear()
            self._queued_ids.clear()
            self._queue_size = 0
            self._role_rotation.clear()
            self._rotation_set.clear()
        # 断线后重新绑定不能被当成“连续运行”，过期活动要保守处理。
        self._advance_times.clear()

    def request_stop(self) -> None:
        self._stopping.set()
        wake, loop = self._wake, self._loop
        if wake is not None and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except Exception:
                pass

    async def stop(self) -> None:
        self._stopping.set()
        wake = self._wake
        if wake is not None:
            try:
                wake.set()
            except Exception:
                pass
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass
        self._worker = None
        self._wake = None
        self._loop = None
        with self._queue_lock:
            self._pending.clear()
            self._queued_ids.clear()
            self._queue_size = 0
            self._active_ids.clear()
            self._role_rotation.clear()
            self._rotation_set.clear()
        self._advance_times.clear()

    def _ensure_worker(self) -> None:
        if self._stopping.is_set():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._worker is not None and not self._worker.done() and self._loop is loop:
            return
        self._loop = loop
        self._wake = asyncio.Event()
        self._worker = loop.create_task(self._worker_loop(), name="RoleLifeWorker")

    async def _worker_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                wake = self._wake
                if wake is None:
                    return
                # 先清再取：避免取出空队列与 clear 之间到达的唤醒被吞掉。
                wake.clear()
                entry = self._pop_next()
                if entry is None:
                    # 没有作业时只等 advance/enqueue 唤醒，不另起周期时钟。
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=300)
                    except asyncio.TimeoutError:
                        pass
                    continue
                try:
                    await self._run_job(*entry)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # 单作业失败不能带走 worker
                    self._log(f"[RoleLife] worker job error: {exc}")
        except asyncio.CancelledError:
            raise

    def _ensure_worker_threadsafe(self) -> None:
        """WebUI/其他线程触发入队时，把 worker 创建调度回引擎 loop。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._ensure_worker)
                except Exception:
                    pass
            return
        self._ensure_worker()

    def _pop_next(self) -> tuple[str, str, str] | None:
        """按角色公平轮转取一个作业：A,B,A,B 交替，轮转表有界。"""
        with self._queue_lock:
            while self._role_rotation:
                key = self._role_rotation.popleft()
                self._rotation_set.discard(key)
                queue = self._pending.get(key)
                if not queue:
                    self._pending.pop(key, None)
                    continue
                job_id = queue.popleft()
                self._queued_ids.discard((key[0], key[1], job_id))
                self._active_ids.add(job_id)  # 与出队原子登记，advance 不会误判重启
                self._queue_size = max(0, self._queue_size - 1)
                if queue:
                    self._role_rotation.append(key)
                    self._rotation_set.add(key)
                else:
                    self._pending.pop(key, None)
                return key[0], key[1], job_id
            # 防御：pending 有队列但轮转表缺失（不应发生），补回后继续。
            if self._pending:
                for key in list(self._pending):
                    if key not in self._rotation_set:
                        self._role_rotation.append(key)
                        self._rotation_set.add(key)
                while self._role_rotation:
                    key = self._role_rotation.popleft()
                    self._rotation_set.discard(key)
                    queue = self._pending.get(key)
                    if not queue:
                        self._pending.pop(key, None)
                        continue
                    job_id = queue.popleft()
                    self._queued_ids.discard((key[0], key[1], job_id))
                    self._active_ids.add(job_id)
                    self._queue_size = max(0, self._queue_size - 1)
                    if queue:
                        self._role_rotation.append(key)
                        self._rotation_set.add(key)
                    else:
                        self._pending.pop(key, None)
                    return key[0], key[1], job_id
        return None

    def _push_jobs(self, jobs: Iterable[tuple[str, str, str]]) -> None:
        pushed = False
        with self._queue_lock:
            for account, role_id, job_id in jobs:
                key = (account, role_id)
                marker = (account, role_id, job_id)
                if marker in self._queued_ids or job_id in self._active_ids:
                    continue
                if self._queue_size >= MAX_TOTAL_QUEUE:
                    break
                queue = self._pending.get(key)
                if queue is None:
                    queue = deque()
                    self._pending[key] = queue
                if len(queue) >= MAX_ROLE_QUEUE:
                    continue
                queue.append(job_id)
                if key not in self._rotation_set:
                    self._role_rotation.append(key)
                    self._rotation_set.add(key)
                self._queued_ids.add(marker)
                self._queue_size += 1
                pushed = True
        if pushed:
            self._signal_worker()

    def _signal_worker(self) -> None:
        wake, loop = self._wake, self._loop
        if wake is None or loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if running is loop:
                wake.set()
            else:
                loop.call_soon_threadsafe(wake.set)
        except Exception:
            pass

    # -- 配置与角色 -------------------------------------------------------
    def _section(self) -> Mapping[str, Any]:
        try:
            section = self._config_getter() or {}
        except Exception as exc:
            self._log(f"[RoleLife] config error: {exc}")
            return {}
        return section if isinstance(section, Mapping) else {}

    def _role_cfg(self, account: str, role_id: str) -> dict[str, Any] | None:
        return role_config(self._section(), account, role_id)

    def _bot_display_name(self) -> str:
        value = self._bot_name
        if callable(value):
            try:
                value = value()
            except Exception:
                value = ""
        return str(value or "")

    @staticmethod
    def _is_global_life(account: str, role_id: str) -> bool:
        return str(account or "") == GLOBAL_ACCOUNT and str(role_id or "") == GLOBAL_ROLE

    @classmethod
    def _store_expect(cls, account: str, role_id: str,
                      epoch: int | None, state_version: int | None,
                      revision: str = "") -> dict[str, Any]:
        expect: dict[str, Any] = {}
        if epoch is not None:
            expect["epoch"] = int(epoch)
        if state_version is not None:
            expect["state_version"] = int(state_version)
        if revision and not cls._is_global_life(account, role_id):
            expect["revision"] = str(revision)
        return expect

    def _persona(self, account: str, role_id: str) -> Persona | None:
        """从运行时 Others 解析指定角色；统一全局角色生活跟随当前激活人设。"""
        try:
            others = self._persona_getter() or {}
        except Exception as exc:
            self._log(f"[RoleLife] persona config error: {exc}")
            return None
        if not isinstance(others, Mapping):
            return None
        try:
            if self._is_global_life(account, role_id):
                from bot.persona import resolve_persona
                try:
                    return resolve_persona(others, bot_name=self._bot_display_name(),
                                           context_type="")
                except ValueError:
                    # 没有任何预设且未填写人设文本时，用最小兜底人格让本地界面仍可查看与编辑
                    return Persona(
                        role_id=GLOBAL_ROLE,
                        raw_prompt=f"你是{self._bot_display_name() or '机器人'}。",
                        revision=hashlib.sha256(
                            f"fallback:{self._bot_display_name()}".encode("utf-8")
                        ).hexdigest()[:16],
                        preset_id=GLOBAL_ROLE,
                        name=self._bot_display_name() or "机器人",
                    )
            presets = others.get("personality_presets", [])
            if not isinstance(presets, list):
                return None
            match = next(
                (p for p in presets
                 if isinstance(p, Mapping) and str(p.get("id", "") or "").strip() == role_id),
                None,
            )
            if role_id == "default" and match is None:
                # 旧 personality_prompt 角色：显式构造“无预设”视图，避免当前
                # 私聊/群聊已切到别的预设时把 default 角色解析成别人的文本。
                prompt = str(others.get("personality_prompt", "") or "").strip()
                if not prompt:
                    return None
                from bot.persona import resolve_persona
                legacy = {
                    "personality_prompt": prompt,
                    "personality_presets": [],
                    "active_personality_preset": "",
                    "private_personality_preset": "",
                    "group_personality_preset": "",
                }
                return resolve_persona(legacy, bot_name=self._bot_display_name())
            if match is None:
                return None
            # 用显式 override 解析，避免“当前私聊已切到别的预设”导致旧角色失效。
            from bot.persona import resolve_persona
            return resolve_persona(others, bot_name=self._bot_display_name(),
                                   context_type="", preset_id_override=role_id)
        except ValueError as exc:
            self._log(f"[RoleLife] persona error {account}/{role_id}: {exc}")
            return None
        except Exception as exc:
            self._log(f"[RoleLife] persona error {account}/{role_id}: {exc}")
            return None

    def _now(self, now: float | None = None) -> float:
        return float(self._clock() if now is None else now)

    def _effective_revision(self, account: str, role_id: str,
                            state: Mapping[str, Any]) -> str:
        """只读/首次操作视图：尚未推进时回退到当前 persona revision。

        统一全局生活切换人设时始终返回当前生效人设的 revision，不因切人设锁死手动操作。
        """
        if self._is_global_life(account, role_id):
            persona = self._persona(account, role_id)
            if persona is not None:
                return persona.revision
        stored = str(state.get("revision") or "")
        if stored:
            return stored
        persona = self._persona(account, role_id)
        return persona.revision if persona is not None else ""

    def _is_current(self, account: str, generation: int) -> bool:
        with self._lifecycle_lock:
            return (not self._stopping.is_set()
                    and self._account == account
                    and self._generation == generation)

    # -- advance ----------------------------------------------------------
    async def advance(self, now: float | None = None,
                      *, accounts: Sequence[str] | None = None) -> None:
        """由现有 tick 调用；最多 await 落盘，不等待任何模型生成。"""
        if self._stopping.is_set():
            return
        now = self._now(now)
        section = self._section()
        if accounts is None:
            with self._lifecycle_lock:
                bound_account = self._account
            if bound_account is None:
                return
            accounts = [bound_account]
        new_jobs: list[tuple[str, str, str]] = []
        roles = section.get("roles") if isinstance(section, Mapping) else None
        for account in accounts:
            try:
                account = validate_account(account)
            except ValueError:
                continue
            if account == GLOBAL_ACCOUNT:
                cfg = resolve_global_config(section)
                try:
                    jobs = await asyncio.to_thread(
                        self._advance_role_sync, GLOBAL_ACCOUNT, GLOBAL_ROLE, dict(cfg), now)
                except Exception as exc:
                    self._log(f"[RoleLife] advance error {GLOBAL_ACCOUNT}/{GLOBAL_ROLE}: {exc}")
                    continue
                new_jobs.extend(jobs)
                continue
            if not isinstance(roles, Mapping):
                continue
            by_role = roles.get(account)
            if not isinstance(by_role, Mapping):
                continue
            for role_id, cfg in by_role.items():
                if not isinstance(cfg, Mapping):
                    continue
                try:
                    jobs = await asyncio.to_thread(
                        self._advance_role_sync, account, str(role_id), dict(cfg), now)
                except Exception as exc:
                    self._log(f"[RoleLife] advance error {account}/{role_id}: {exc}")
                    continue
                new_jobs.extend(jobs)
        if new_jobs:
            self._ensure_worker()
            self._push_jobs(new_jobs)

    def _advance_role_sync(self, account: str, role_id: str,
                           cfg: dict[str, Any], now: float) -> list[tuple[str, str, str]]:
        # 关闭的功能不碰状态文件：不创建目录、不 fsync、不排任何作业。
        if not cfg.get("enabled"):
            self._advance_times.pop((account, role_id), None)
            return []
        new_jobs: list[tuple[str, str, str]] = []
        generation = self._generation
        key = (account, role_id)
        previous_seen = self._advance_times.get(key)

        def mutate(state: dict[str, Any]) -> None:
            snapshot = copy.deepcopy(state)
            clock = _Clock(cfg["timezone"])
            persona = self._persona(account, role_id)
            if persona is None:
                metrics = state.setdefault("metrics", {})
                if metrics.get("last_error") != "人设解析失败":
                    metrics["last_error"] = "人设解析失败"
                    metrics["last_error_at"] = now
                return
            # 连续运行判定只依赖本进程内存：重启后 previous_seen 为 None，
            # 一律按“恢复”保守处理，不靠高频落盘证明在线。
            gap = None if previous_seen is None else now - previous_seen
            continuous = gap is not None and 0 <= gap <= ADVANCE_GAP_SECONDS
            state["revision"] = persona.revision
            state["config_revision"] = config_revision(cfg)
            prune_runtime_state(state, now, int(cfg["retention_days"]))
            self._reconcile_jobs(state, now)
            self._reconcile_days(state, clock, now)
            sleeping = self._sync_sleep(state, cfg, clock, now, new_jobs, generation)
            today = clock.date_str(now)
            plan = (state.get("plans") or {}).get(today)
            if plan is not None:
                self._progress_plan(state, plan, cfg, clock, now, sleeping, continuous,
                                    new_jobs, generation, persona)
            if (not sleeping and cfg.get("plan_enabled") and plan is None
                    and now < clock.next_day_start(today) - 3600):
                plan_args: dict[str, Any] = {
                    "date": today,
                    "current_time": clock.local(now).strftime("%H:%M"),
                }
                today_ctx = _today_plan_context(state, today, clock)
                if today_ctx:
                    plan_args["plan_context"] = today_ctx
                notes = _recent_life_notes(state, clock, today)
                if notes:
                    plan_args["life_notes"] = notes
                self._make_job(state, account, role_id, "plan", persona, generation,
                               now, plan_args, f"plan:{today}", new_jobs)
            if (not sleeping and cfg.get("diary_enabled")
                    and now >= clock.hhmm(today, cfg["diary_time"])
                    and not any(e.get("kind") == "diary" and e.get("date") == today
                                for e in state.get("experiences", []))):
                material = self._diary_material(state, today)
                if material:
                    self._make_job(state, account, role_id, "diary", persona, generation,
                                   now, {"date": today, "material": material},
                                   f"diary:{today}", new_jobs)
            if not sleeping and cfg.get("maintenance_enabled"):
                slot = int(now // max(60, int(cfg["maintenance_interval_seconds"])))
                self._make_job(state, account, role_id, "maintenance", persona, generation,
                               now, {"date": today, "material": self._recent_material(state)},
                               f"maintenance:{slot}", new_jobs)
            # last_advance 只随真实内容变化一起落盘，不单独制造周期写。
            if state != snapshot:
                state["last_advance"] = now

        try:
            state = self.store.update(account, role_id, mutate)
        except RoleLifeCorruptError as exc:
            # 损坏状态只暂停、不覆盖：原字节保持不变，等管理员明确清空。
            self._log(f"[RoleLife] state corrupt {account}/{role_id}: {exc}")
            return []
        self._advance_times[key] = now
        # 恢复进程/连接中断时尚未发过请求的 pending 作业；attempts>0 的请求
        # 结果不可知，绝不自动重放（_reconcile_jobs 已把 running 标 unknown）。
        for job_id, job in (state.get("jobs") or {}).items():
            if (isinstance(job, dict) and job.get("status") == "pending"
                    and int(job.get("attempts") or 0) == 0):
                new_jobs.append((account, role_id, job_id))
        return new_jobs

    def _reconcile_jobs(self, state: dict[str, Any], now: float) -> None:
        jobs = state.get("jobs")
        if not isinstance(jobs, dict):
            return
        with self._queue_lock:
            active = set(self._active_ids)
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            if job.get("id") in active:
                continue  # 本进程 worker 正在跑，不能当成重启遗留
            if job.get("status") == "running":
                # 进程重启时 running 没有可见的最终回执：模型请求可能已发出计费。
                job["status"] = "unknown"
                job["error"] = "interrupted_on_restart"
                job["finished_at"] = now

    def _reconcile_days(self, state: dict[str, Any], clock: _Clock, now: float) -> None:
        today = clock.date_str(now)
        plans = state.get("plans")
        if not isinstance(plans, dict):
            return
        for day, plan in plans.items():
            if day >= today or not isinstance(plan, dict):
                continue
            for item in plan.get("items", []):
                if item.get("status") in ("planned", "active"):
                    # 多日离线不补编：过去的活动只标跳过/中断，不生成经历。
                    item["status"] = "interrupted" if item.get("status") == "active" else "skipped"
                    item["ended_at"] = now

    def _sleep_segments(self, cfg: dict[str, Any], clock: _Clock, now: float,
                        *, include_tomorrow: bool = False
                        ) -> list[tuple[str, float, float]]:
        today = clock.date_str(now)
        deltas = (-1, 0, 1) if include_tomorrow else (-1, 0)
        segments = []
        for delta in deltas:
            day = clock.add_days(today, delta)
            start = clock.hhmm(day, cfg["sleep_start"])
            end = clock.plus_hours(start, int(cfg["sleep_hours"]))
            segments.append((day, start, end))
        return segments

    def _current_sleep(self, cfg: dict[str, Any], clock: _Clock, now: float
                       ) -> tuple[str, float, float] | None:
        if not cfg.get("sleep_enabled"):
            return None
        for segment in reversed(self._sleep_segments(cfg, clock, now)):
            if segment[1] <= now < segment[2]:
                return segment
        return None

    def _sync_sleep(self, state: dict[str, Any], cfg: dict[str, Any], clock: _Clock,
                    now: float, new_jobs: list, generation: int | None) -> bool:
        if not cfg.get("sleep_enabled"):
            # 关闭睡眠后旧睡段不能继续触发“醒后梦”，也不表示角色在睡。
            state["sleep"] = {}
            return False
        current = self._current_sleep(cfg, clock, now)
        sleeping = current is not None
        stored = state.get("sleep") if isinstance(state.get("sleep"), dict) else {}
        if current is not None:
            day, start, end = current
            if stored.get("start") != start or stored.get("end") != end:
                same_sleep = stored.get("start") == start
                state["sleep"] = {
                    "date": day,
                    "start": start,
                    "end": end,
                    "wake_dream_done": bool(stored.get("wake_dream_done")) if same_sleep else False,
                }
            return True
        # 刚醒：只处理仍有一次意义的一次醒后任务。
        if stored and float(stored.get("end") or 0) <= now and not stored.get("wake_dream_done"):
            wake_end = float(stored.get("end") or 0)
            state["sleep"] = dict(stored, wake_dream_done=True)
            if cfg.get("dream_enabled") and now - wake_end <= 3 * 3600:
                persona = self._persona(state.get("account", ""), state.get("role_id", ""))
                account = state.get("account", "")
                role_id = state.get("role_id", "")
                if persona is not None and account and role_id:
                    material = self._recent_material(state, limit=6)
                    self._make_job(
                        state, account, role_id, "dream", persona, generation, now,
                        {"sleep_start": stored.get("start"), "sleep_end": wake_end,
                         "material": material},
                        f"dream:{int(stored.get('start') or 0)}", new_jobs)
        return False

    def _progress_plan(self, state: dict[str, Any], plan: dict[str, Any],
                       cfg: dict[str, Any], clock: _Clock, now: float, sleeping: bool,
                       continuous: bool, new_jobs: list, generation: int | None,
                       persona: Persona) -> None:
        account = state.get("account", "")
        role_id = state.get("role_id", "")
        experiences = state.get("experiences", [])
        for item in plan.get("items", []):
            status = item.get("status")
            experience_id = next(
                (e.get("id") for e in experiences
                 if e.get("kind") == "activity" and item.get("id") in (e.get("source_ids") or [])),
                "",
            )
            if experience_id:
                item["status"] = "completed"
                item["experience_id"] = experience_id
                continue
            if status in ("completed", "skipped", "interrupted"):
                continue
            start, end = float(item.get("start") or 0), float(item.get("end") or 0)
            if now < start:
                continue
            if now < end:
                if item.get("status") != "active":
                    # 当前仍在活动窗口内：可以进入进行中，但这只是计时状态。
                    item["status"] = "active"
                    item["started_at"] = now
                continue
            # 已过结束时间：
            if status == "planned":
                # 从未真实推进到 active（离线/掉 tick），不补编经历。
                item["status"] = "skipped"
                item["ended_at"] = now
                continue
            # 曾经 active 且现在已结束：只有连续运行才允许排“结束经历”作业。
            if not continuous:
                item["status"] = "interrupted"
                item["ended_at"] = now
                continue
            if sleeping or not cfg.get("activity_enabled"):
                item["status"] = "interrupted"
                item["ended_at"] = now
                continue
            item_jobs = [
                j for j in (state.get("jobs") or {}).values()
                if isinstance(j, Mapping) and j.get("kind") == "activity"
                and j.get("dedupe_key") == f"activity:{item.get('id')}"
            ]
            if any(j.get("status") in ("failed", "unknown") for j in item_jobs) and not any(
                j.get("status") in ("pending", "running") for j in item_jobs
            ):
                item["status"] = "interrupted"
                item["ended_at"] = now
                continue
            item["status"] = "active"
            item.setdefault("started_at", start)
            self._make_job(
                state, account, role_id, "activity", persona, generation, now,
                {"date": clock.date_str(now), "plan_serial": int(plan.get("serial") or 0),
                 "item": {"id": item.get("id"), "title": item.get("title"),
                          "purpose": item.get("purpose"), "start": start, "end": end},
                 "plan_context": [{"title": i.get("title"), "purpose": i.get("purpose")}
                                  for i in plan.get("items", [])][:MAX_PLAN_ITEMS]},
                f"activity:{item.get('id')}", new_jobs)

    def _recent_material(self, state: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
        experiences = [e for e in state.get("experiences", [])
                       if e.get("kind") in ("activity", "diary", "dream")]
        experiences.sort(key=lambda e: e.get("created_at", 0), reverse=True)
        return [{"kind": e.get("kind"), "date": e.get("date"),
                 "text": e.get("text", "")[:400]} for e in experiences[:limit]]

    def _diary_material(self, state: dict[str, Any], today: str) -> list[dict[str, Any]]:
        """日记只接受已落盘材料；计划条目带 id/status，明确“计划≠完成”。"""
        material = [{"kind": "activity", "id": e.get("id"), "status": "completed",
                     "date": e.get("date"), "text": e.get("text", "")[:600]}
                    for e in state.get("experiences", [])
                    if e.get("kind") == "activity" and e.get("date") == today]
        plan = (state.get("plans") or {}).get(today)
        if isinstance(plan, dict):
            for item in plan.get("items", []):
                if item.get("status") in ("planned", "active", "completed"):
                    material.append({"kind": "plan", "id": item.get("id"),
                                     "status": item.get("status"), "date": today,
                                     "text": str(item.get("title") or "")[:120]})
        for note in [n for n in state.get("life_notes", [])
                     if isinstance(n, Mapping) and n.get("date") == today
                     and n.get("source") == "manual" and not n.get("scope")][-4:]:
            material.append({"kind": "note", "id": note.get("id"), "date": today,
                             "text": str(note.get("text") or "")[:120]})
        return material[:12]

    @staticmethod
    def _remember_dedupe(state: dict[str, Any], dedupe_key: str) -> None:
        if not dedupe_key:
            return
        keys = state.setdefault("dedupe", [])
        if dedupe_key not in keys:
            keys.append(dedupe_key)
            del keys[:-MAX_DEDUPE_KEYS_STATE]

    @staticmethod
    def _evict_jobs_for_capacity(state: dict[str, Any]) -> bool:
        """为新增作业腾出历史空间；绝不删除 pending/running/unknown。

        已终结的 succeeded/failed 记录按时间从旧到新淘汰，其 dedupe 键
        留在 state["dedupe"] 中，防止同一日程/活动被重新排一次。
        """
        jobs = state.setdefault("jobs", {})
        order = state.setdefault("job_order", [])
        while len(order) >= MAX_JOB_HISTORY:
            victim = None
            for wanted in (("succeeded", "failed"), ("unknown",)):
                for job_id in order:
                    job = jobs.get(job_id)
                    if isinstance(job, dict) and job.get("status") in wanted:
                        victim = job_id
                        break
                if victim is not None:
                    break
            if victim is None:
                return False
            job = jobs.pop(victim, None)
            order.remove(victim)
            if isinstance(job, dict):
                RoleLifeEngine._remember_dedupe(state, str(job.get("dedupe_key") or ""))
        return True

    def _make_job(self, state: dict[str, Any], account: str, role_id: str, kind: str,
                  persona: Persona, generation: int | None, now: float,
                  args: dict[str, Any], dedupe_key: str, new_jobs: list) -> str | None:
        jobs = state.setdefault("jobs", {})
        order = state.setdefault("job_order", [])
        if dedupe_key in (state.get("dedupe") or []):
            # 已执行过的自动作业不再重复入队（含历史被淘汰后）。
            return None
        for job in jobs.values():
            if isinstance(job, dict) and job.get("dedupe_key") == dedupe_key:
                # 自动作业只入队一次；失败后需要 WebUI 手动 retry_job。
                return None
        if not self._evict_jobs_for_capacity(state):
            return None
        job_id = _job_id(account, role_id, kind, dedupe_key, int(now))
        jobs[job_id] = {
            "id": job_id, "kind": kind, "status": "pending",
            "created_at": now, "started_at": 0, "finished_at": 0,
            "args": copy.deepcopy(args), "error": "", "result_id": "",
            "revision": persona.revision,
            "config_revision": str(state.get("config_revision") or ""),
            "epoch": int(state.get("epoch") or 0),
            "generation": int(generation or 0), "attempts": 0,
            "dedupe_key": dedupe_key, "request_id": "",
        }
        order.append(job_id)
        new_jobs.append((account, role_id, job_id))
        return job_id

    # -- 提交模型结果 -----------------------------------------------------
    async def _run_job(self, account: str, role_id: str, job_id: str) -> None:
        try:
            cfg = self._role_cfg(account, role_id)
            if not cfg or not cfg.get("enabled"):
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "failed", "disabled", "")
                return
            persona = self._persona(account, role_id)
            try:
                snapshot = await asyncio.to_thread(self.store.load, account, role_id)
            except RoleLifeCorruptError as exc:
                self._log(f"[RoleLife] corrupt state, job skipped {account}/{role_id}: {exc}")
                return
            job = (snapshot.get("jobs") or {}).get(job_id)
            if not isinstance(job, dict) or job.get("status") != "pending":
                return
            if (not self._is_current(account, int(job.get("generation") or 0))
                    or persona is None
                    or persona.revision != job.get("revision")
                    or str(job.get("config_revision") or "") != config_revision(cfg)
                    or int(snapshot.get("epoch") or 0) != int(job.get("epoch") or 0)):
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "failed", "stale", "")
                return
            manual_texts = {
                str(n.get("text") or "")[:120]
                for n in (snapshot.get("life_notes") or [])
                if isinstance(n, Mapping)
                and n.get("source") == "manual"
                and not n.get("scope")
                and n.get("text")
            }
            await self._run_job_inner(
                account, role_id, job_id, cfg, persona, job, manual_texts=manual_texts,
            )
        finally:
            # 任何提前返回都必须释放 active 标记，否则 advance 会一直把它
            # 当成“本进程正在跑”，重启遗留检测和清空语义都会失真。
            with self._queue_lock:
                self._active_ids.discard(job_id)

    async def _run_job_inner(self, account: str, role_id: str, job_id: str,
                             cfg: dict[str, Any], persona: Persona,
                             job: dict[str, Any], *,
                             manual_texts: set[str] | None = None) -> None:
        if not await asyncio.to_thread(self._mark_running, account, role_id, job_id):
            # 预发送状态没写成功：不能发请求，也不能留下可被重启重放的 pending。
            return
        job_args = copy.deepcopy(job.get("args") or {})
        kind = str(job.get("kind") or "")
        if manual_texts is not None:
            if kind == "plan" and isinstance(job_args.get("life_notes"), list):
                filtered_notes = [
                    x for x in job_args["life_notes"]
                    if isinstance(x, str) and x in manual_texts
                ]
                if filtered_notes:
                    job_args["life_notes"] = filtered_notes
                else:
                    job_args.pop("life_notes", None)
            elif kind == "diary" and isinstance(job_args.get("material"), list):
                job_args["material"] = [
                    e for e in job_args["material"]
                    if not (
                        isinstance(e, Mapping)
                        and e.get("kind") == "note"
                        and str(e.get("text") or "")[:120] not in manual_texts
                    )
                ]
        life_job = LifeJob(
            job_id=job_id, account=account, role_id=role_id,
            role_key=persona_role_key(role_id), kind=kind,
            revision=persona.revision,
            config_revision=str(job.get("config_revision") or ""),
            epoch=int(job.get("epoch") or 0),
            generation=int(job.get("generation") or 0),
            persona_template=persona.raw_prompt,
            config=copy.deepcopy(cfg), args=job_args,
        )
        schema_attempts = 0
        last_error = ""
        retried_request = False
        while schema_attempts < 2:
            schema_attempts += 1
            if schema_attempts > 1:
                # 第二次真实请求：审计计数与预算同步（预算在 reserve 里独立扣）。
                if not await asyncio.to_thread(self._bump_attempt, account, role_id, job_id):
                    return
            denied = [False]
            reserve = self._reserve_attempt_async(account, role_id, cfg, life_job)

            async def reserve_attempt() -> bool:
                ok = await reserve()
                if not ok:
                    denied[0] = True
                return ok

            try:
                result = await self._generate(life_job, reserve_attempt)
            except LifeBudgetDenied:
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "failed", "budget_denied", "")
                return
            except LifeNotSent:
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "failed", "not_sent", "")
                return
            except LifeRequestRejected as exc:
                if not retried_request:
                    retried_request = True
                    last_error = str(exc)[:200] or "request_rejected"
                    continue
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "failed", last_error, "")
                return
            except asyncio.CancelledError:
                # 取消不等于上游没计费：按 unknown 记账后立刻退出；直接同步写，
                # 避免取消路径上的再次 await 被吞掉导致账本缺失。
                self._finish_job(account, role_id, job_id, "unknown", "cancelled", "")
                raise
            except LifeGenerationUnknown as exc:
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "unknown", str(exc)[:200] or "unknown", "")
                return
            except (TimeoutError, asyncio.TimeoutError) as exc:
                # 超时可能已在上游计费：结果未知，不自动重试。
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "unknown", f"timeout:{exc}"[:200], "")
                return
            except Exception as exc:
                # 未分类异常默认视为“可能已发出”：unknown，绝不自动重放。
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "unknown", str(exc)[:200] or "generate_failed", "")
                return
            if result is None:
                # 适配器未发出请求：区分额度/存储拒绝与普通无输出。
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "failed",
                                        "budget_denied" if denied[0] else "not_sent", "")
                return
            error = await asyncio.to_thread(
                self._apply_result, account, role_id, job_id, life_job, result)
            if error and error.startswith("store_failed"):
                # 已有有效模型结果但短暂写盘失败：绝不重调模型再次生成，仅补写一次已有结果。
                error = await asyncio.to_thread(
                    self._apply_result, account, role_id, job_id, life_job, result)
                if error:
                    await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                            "failed", error[:200], "")
                    return
                return
            if not error:
                return
            last_error = error
            if error.startswith("stale"):
                await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                        "failed", error, "")
                return
        await asyncio.to_thread(self._finish_job, account, role_id, job_id,
                                "failed", last_error[:200] or "invalid_output", "")

    def _bump_attempt(self, account: str, role_id: str, job_id: str) -> bool:
        def mutate(state: dict[str, Any]) -> None:
            job = (state.get("jobs") or {}).get(job_id)
            if isinstance(job, dict) and job.get("status") != "running":
                raise _StaleJob("job is not running")
            if isinstance(job, dict):
                job["attempts"] = int(job.get("attempts") or 0) + 1
        try:
            self.store.update(account, role_id, mutate)
        except Exception as exc:
            self._log(f"[RoleLife] bump_attempt failed: {exc}")
            return False
        return True

    def _mark_running(self, account: str, role_id: str, job_id: str) -> bool:
        """预发送状态必须落盘成功，否则不得发出任何真实请求。"""
        now = self._now()
        def mutate(state: dict[str, Any]) -> None:
            job = (state.get("jobs") or {}).get(job_id)
            if not isinstance(job, dict) or job.get("status") != "pending":
                raise _StaleJob("job is not pending")
            job["status"] = "running"
            job["started_at"] = now
            job["attempts"] = int(job.get("attempts") or 0) + 1
        try:
            self.store.update(account, role_id, mutate)
        except Exception as exc:
            self._log(f"[RoleLife] mark_running failed, request not sent: {exc}")
            return False
        return True

    def _finish_job(self, account: str, role_id: str, job_id: str,
                    status: str, error: str, result_id: str) -> None:
        now = self._now()
        def mutate(state: dict[str, Any]) -> None:
            job = (state.get("jobs") or {}).get(job_id)
            if not isinstance(job, dict) or job.get("status") in JOB_FINISHED:
                return
            job["status"] = status
            job["error"] = error[:300]
            job["finished_at"] = now
            if result_id:
                job["result_id"] = result_id
            metrics = state.setdefault("metrics", {})
            if status == "succeeded":
                metrics["last_success_at"] = now
            else:
                metrics["last_error"] = error[:300]
                metrics["last_error_at"] = now
                if job.get("kind") == "activity" and status in ("failed", "unknown"):
                    target_id = str(((job.get("args") or {}).get("item") or {}).get("id") or "").strip()
                    if not target_id and str(job.get("dedupe_key") or "").startswith("activity:"):
                        target_id = str(job.get("dedupe_key") or "")[len("activity:"):]
                    if target_id:
                        for plan in (state.get("plans") or {}).values():
                            if not isinstance(plan, dict):
                                continue
                            for pitem in plan.get("items") or []:
                                if (
                                    isinstance(pitem, dict)
                                    and pitem.get("id") == target_id
                                    and pitem.get("status") == "active"
                                    and float(pitem.get("end") or 0) <= now
                                ):
                                    pitem["status"] = "interrupted"
                                    pitem["ended_at"] = now
        try:
            self.store.update(account, role_id, mutate)
        except Exception as exc:
            self._log(f"[RoleLife] finish_job failed: {exc}")

    def _reserve_attempt_async(self, account: str, role_id: str,
                               cfg: dict[str, Any], job: LifeJob | None = None
                               ) -> Callable[[], Awaitable[bool]]:
        """给模型适配器的扣额度入口。适配器必须 `await` 后才可发请求。

        扣额度前在同一个受管线程里复核账号代次/epoch/persona/config/作业状态：
        排队后若已清空、换号、改人设或改配置，直接拒绝，不花钱生成。
        """
        async def reserve() -> bool:
            return await asyncio.to_thread(
                self._reserve_attempt, account, role_id, cfg, job)
        return reserve

    def _job_still_valid(self, account: str, role_id: str, job: LifeJob,
                         state: dict[str, Any]) -> bool:
        record = (state.get("jobs") or {}).get(job.job_id)
        if not isinstance(record, dict) or record.get("status") != "running":
            return False
        if int(state.get("epoch") or 0) != job.epoch:
            return False
        if (state.get("revision") or "") != job.revision:
            return False
        if (state.get("config_revision") or "") != job.config_revision:
            return False
        if self._role_cfg(account, role_id) != job.config:
            return False
        if not self._is_current(account, job.generation):
            return False
        persona = self._persona(account, role_id)
        return persona is not None and persona.revision == job.revision

    def _config_matches(self, account: str, role_id: str, job: LifeJob) -> bool:
        cfg = self._role_cfg(account, role_id)
        return bool(cfg and cfg.get("enabled")
                    and config_revision(cfg) == job.config_revision)

    def _reserve_attempt(self, account: str, role_id: str, cfg: dict[str, Any],
                         job: LifeJob | None = None) -> bool:
        """角色额度 + 共享账号额度各扣一次；任何落盘失败都返回 False，不发请求。"""
        now = self._now()
        try:
            snapshot = self.store.load(account, role_id)
        except RoleLifeCorruptError:
            return False  # 坏账本不能当作“额度未用”；等管理员清空
        if job is not None and not self._job_still_valid(account, role_id, job, snapshot):
            return False
        if float(snapshot.get("budget_unknown_until") or 0) > now:
            # 损坏恢复后原用量不可知：保护期内不允许本角色再消耗额度。
            return False
        try:
            if not self._reserve_account(account):
                return False
        except Exception:
            return False
        def mutate(state: dict[str, Any]) -> None:
            if job is not None and not self._job_still_valid(account, role_id, job, state):
                raise _StaleJob("job invalid at reservation")
            budget = state.setdefault("budget", {"calls": [], "role_daily_limit": 24})
            budget.setdefault("role_daily_limit", 24)
            budget["calls"] = [t for t in budget.get("calls", []) if now - t < 86400] + [now]
        try:
            self.store.update(account, role_id, mutate)
        except Exception:
            # 保守：账号额度已扣，请求仍不发出；宁可少生成也不绕限。
            return False
        return True

    def _apply_result(self, account: str, role_id: str, job_id: str,
                      job: LifeJob, result: Any) -> str:
        """校验并提交；返回空串成功，否则返回可展示的错误。"""
        now = self._now()
        if (not self._is_current(account, job.generation) or self._stopping.is_set()):
            # 先做一次不进锁的快速拒绝；真正的原子提交校验仍在 mutator 里。
            return "stale:connection_changed"
        cfg = job.config
        clock = _Clock(cfg["timezone"])
        error_box = [""]
        result_id_box = [""]
        # 配置 revision 在进入锁前、锁内各校验一次：锁外是快速拒绝，锁内保证
        # 落盘瞬间配置仍未变（热改配置后旧回包不能复活）。
        if not self._config_matches(account, role_id, job):
            return "stale:config_changed"

        def mutate(state: dict[str, Any]) -> None:
            record = (state.get("jobs") or {}).get(job_id)
            if not isinstance(record, dict):
                error_box[0] = "stale:job_missing"
                return
            if record.get("status") != "running":
                error_box[0] = "stale:job_state"
                return
            stored_revision = state.get("revision") or ""
            stored_config = state.get("config_revision") or ""
            if (int(state.get("epoch") or 0) != job.epoch
                    or (stored_revision and stored_revision != job.revision)
                    or (stored_config and stored_config != job.config_revision)):
                error_box[0] = "stale:state_changed"
                return
            if not self._is_current(account, job.generation) or self._stopping.is_set():
                error_box[0] = "stale:connection_changed"
                return
            current_cfg = role_config(self._section(), account, role_id)
            if not current_cfg or not current_cfg.get("enabled"):
                error_box[0] = "stale:disabled"
                return
            if config_revision(current_cfg) != job.config_revision:
                error_box[0] = "stale:config_changed"
                return
            persona = self._persona(account, role_id)
            if persona is None or persona.revision != job.revision:
                error_box[0] = "stale:revision_changed"
                return
            kind = job.kind
            if kind == "plan":
                error = self._apply_plan(state, job, result, clock, now)
            elif kind == "activity":
                error = self._apply_activity(state, job, result, now)
            elif kind == "diary":
                error = self._apply_diary(state, job, result, now)
            elif kind == "dream":
                error = self._apply_dream(state, job, result, now)
            elif kind == "maintenance":
                error = self._apply_maintenance(state, job, result, now)
            else:
                error = "unknown_kind"
            if error:
                error_box[0] = error
                return
            record["status"] = "succeeded"
            record["finished_at"] = now
            record["error"] = ""
            if result_id_box[0]:
                record["result_id"] = result_id_box[0]
            state.setdefault("metrics", {})["last_success_at"] = now

        try:
            self.store.update(account, role_id, mutate)
        except Exception as exc:
            return f"store_failed:{exc}"[:200]
        return error_box[0]

    def _apply_plan(self, state: dict[str, Any], job: LifeJob, result: Any,
                    clock: _Clock, now: float) -> str:
        if not isinstance(result, Mapping):
            return "invalid:plan_not_object"
        raw_items = result.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return "invalid:plan_items"
        day = str(job.args.get("date") or clock.date_str(now))
        if not _valid_date(day):
            return "invalid:plan_date"
        cfg = job.config
        existing = (state.get("plans") or {}).get(day)
        if existing is not None and not job.args.get("replace"):
            return "stale:plan_exists"
        history: list[dict[str, Any]] = []
        if existing is not None:
            # “重排今天剩余安排”：已开始/已完成等历史条目不能抹掉，新条目
            # 不得与历史重叠；历史 ID 原样保留，且总条数受 MAX_PLAN_ITEMS 约束。
            history = [copy.deepcopy(i) for i in existing.get("items", [])
                       if i.get("status") in ("active", "completed", "interrupted", "skipped")]
        limit = min(MAX_PLAN_ITEMS, int(cfg["activities_per_day"]), max(0, MAX_PLAN_ITEMS - len(history)))
        if limit <= 0:
            return "invalid:plan_empty"
        # 目标是某一天的完整日程：睡段按每日本地 HH:MM 计算，避免跨夏令时偏移 1 小时。
        sleep_hours = int(cfg["sleep_hours"])
        sleeping_ranges: list[tuple[float, float]] = []
        if cfg.get("sleep_enabled"):
            for delta in (-1, 0, 1):
                s_day = clock.add_days(day, delta)
                s_start = clock.hhmm(s_day, cfg["sleep_start"])
                sleeping_ranges.append((s_start, clock.plus_hours(s_start, sleep_hours)))
        day_start, day_end = clock.day_start(day), clock.next_day_start(day)
        items = []
        for index, raw in enumerate(raw_items[:MAX_PLAN_ITEMS * 2]):
            if not isinstance(raw, Mapping):
                continue
            title = str(raw.get("title", "") or "").strip()
            purpose = str(raw.get("purpose", "") or "").strip()
            if not title or len(title) > 80 or len(purpose) > 200:
                continue
            try:
                start = clock.hhmm(day, _parse_hhmm(raw.get("start"), "start"))
                end = clock.hhmm(day, _parse_hhmm(raw.get("end"), "end"))
            except ValueError:
                continue
            if not (day_start <= start < end <= day_end):
                continue
            if any(start < sleep_end and end > sleep_start
                   for sleep_start, sleep_end in sleeping_ranges):
                continue
            if end <= now:
                continue
            if any(start < h["end"] and end > h["start"] for h in history):
                continue
            if any(start < other["end"] and end > other["start"] for other in items):
                continue
            items.append({
                "title": title, "purpose": purpose,
                "start": start, "end": end, "status": "planned",
                "origin": "role_simulation", "started_at": 0, "ended_at": 0,
                "experience_id": "",
            })
            if len(items) >= limit:
                break
        if not items:
            return "invalid:plan_empty"
        serial = int(state.get("plan_serial") or 0) + 1
        for index, item in enumerate(items):
            # occurrence 级唯一 ID：重排后旧 activity job 不会认领新条目。
            item["id"] = f"{day}-p{serial}-{index + 1}"
        state["plan_serial"] = serial
        state.setdefault("plans", {})[day] = {
            "plan_id": f"{day}-p{serial}", "serial": serial,
            "generated_at": now,
            "items": sorted(history + items, key=lambda i: i["start"])
            if existing is not None else items,
        }
        return ""

    def _apply_activity(self, state: dict[str, Any], job: LifeJob, result: Any,
                        now: float) -> str:
        text = str((result or {}).get("text", "") if isinstance(result, Mapping) else "").strip()
        if not text:
            return "invalid:empty_experience"
        item_id = str((job.args.get("item") or {}).get("id") or "")
        if not item_id:
            return "invalid:item"
        plan = None
        expected_serial = job.args.get("plan_serial")
        item_serial_tag = f"-p{int(expected_serial)}-" if expected_serial is not None else ""
        for plan_day in (state.get("plans") or {}).values():
            if not isinstance(plan_day, dict):
                continue
            if expected_serial is not None and int(plan_day.get("serial") or 0) != int(expected_serial):
                if not item_serial_tag or item_serial_tag not in item_id:
                    continue  # 计划已被重排：旧作业不能认领新 occurrence
            if any(i.get("id") == item_id for i in plan_day.get("items", [])):
                plan = plan_day
                item = next(i for i in plan_day["items"] if i.get("id") == item_id)
                break
        if plan is None:
            return "stale:item_missing"
        if item.get("status") == "skipped" or (
                item.get("status") == "interrupted" and not job.args.get("manual")):
            return "stale:item_missing"
        exp_id = f"exp-{item_id}"
        if any(e.get("id") == exp_id for e in state.get("experiences", [])):
            item["status"] = "completed"
            item["experience_id"] = exp_id
            _apply_optional_mood(state, result, exp_id, now)
            return ""
        state.setdefault("experiences", []).append({
            "id": exp_id, "kind": "activity", "date": job.args.get("date") or "",
            "created_at": now, "text": text[:1200], "source_ids": [item_id],
            "origin": "role_simulation", "dream": False,
        })
        item["status"] = "completed"
        item["experience_id"] = exp_id
        item["ended_at"] = now
        _apply_optional_mood(state, result, exp_id, now)
        return ""

    def _apply_diary(self, state: dict[str, Any], job: LifeJob, result: Any,
                     now: float) -> str:
        text = str((result or {}).get("text", "") if isinstance(result, Mapping) else "").strip()
        if not text:
            return "invalid:empty_diary"
        day = str(job.args.get("date") or "")
        if not _valid_date(day):
            return "invalid:diary_date"
        exp_id = f"diary-{day}"
        if any(e.get("id") == exp_id for e in state.get("experiences", [])):
            return ""
        state.setdefault("experiences", []).append({
            "id": exp_id, "kind": "diary", "date": day, "created_at": now,
            "text": text[:1600], "source_ids": [f"plan:{day}"],
            "origin": "role_simulation", "dream": False,
        })
        _apply_optional_mood(state, result, exp_id, now)
        return ""

    def _apply_dream(self, state: dict[str, Any], job: LifeJob, result: Any,
                     now: float) -> str:
        text = str((result or {}).get("text", "") if isinstance(result, Mapping) else "").strip()
        if not text:
            return "invalid:empty_dream"
        sleep_start = job.args.get("sleep_start")
        marker = f"sleep:{int(float(sleep_start or 0))}"
        exp_id = f"dream-{int(float(sleep_start or 0))}"
        if any(e.get("id") == exp_id for e in state.get("experiences", [])):
            return ""
        state.setdefault("experiences", []).append({
            "id": exp_id, "kind": "dream", "date": _Clock(job.config["timezone"]).date_str(now),
            "created_at": now, "text": text[:800], "source_ids": [marker],
            "origin": "role_simulation", "dream": True,
        })
        _apply_optional_mood(state, result, exp_id, now)
        return ""

    def _apply_maintenance(self, state: dict[str, Any], job: LifeJob, result: Any,
                           now: float) -> str:
        if not isinstance(result, Mapping):
            return "invalid:maintenance"
        mood = str(result.get("mood", "") or "").strip()[:40]
        energy = str(result.get("energy", "") or "").strip()[:40]
        if not mood and not energy:
            return "invalid:mood_empty"
        if not mood and energy:
            prev_mood = state.get("mood") if isinstance(state.get("mood"), Mapping) else {}
            mood = str(prev_mood.get("mood") or "").strip()[:40] or "平稳"
        state["mood"] = {"mood": mood, "energy": energy, "source_ids": [job.job_id],
                         "updated_at": now}
        return ""

    # -- 只读视图 / WebUI 操作 -------------------------------------------
    def snapshot(self, account: str, role_id: str, now: float | None = None, *,
                 scope: str | None = None) -> dict[str, Any]:
        now = self._now(now)
        cfg = self._role_cfg(account, role_id)
        state, error = self.store.try_load(account, role_id)
        if state is None:
            return {"enabled": bool(cfg and cfg.get("enabled")), "available": False,
                    "corrupt": True, "error": error or "状态文件不可读",
                    "account": account, "role_id": role_id}
        if not cfg:
            return {"enabled": False, "available": False, "corrupt": False,
                    "revision": self._effective_revision(account, role_id, state),
                    "state_version": int(state.get("state_version") or 0),
                    "epoch": int(state.get("epoch") or 0),
                    "last_advance": state.get("last_advance") or 0,
                    "budget_unknown": float(state.get("budget_unknown_until") or 0) > now,
                    "budget_unknown_until": float(state.get("budget_unknown_until") or 0),
                    "recovered_from_corrupt": bool(state.get("recovered_from_corrupt")),
                    "corrupt_backup": state.get("corrupt_backup") or "",
                    "account": account, "role_id": role_id}
        clock = _Clock(cfg["timezone"])
        today = clock.date_str(now)
        plan = (state.get("plans") or {}).get(today) or {}
        current = next(
            (i for i in plan.get("items", [])
             if i.get("status") == "active"
             and float(i.get("start") or 0) <= now < float(i.get("end") or 0)),
            None,
        )
        current_sleep = self._current_sleep(cfg, clock, now)
        sleeping = current_sleep is not None
        if not cfg.get("sleep_enabled"):
            sleep_info = None
            recent_woke_up = False
        elif sleeping:
            _sday, s_start, s_end = current_sleep
            stored_sleep = state.get("sleep") if isinstance(state.get("sleep"), dict) else None
            if (
                stored_sleep
                and float(stored_sleep.get("start") or 0) == s_start
                and float(stored_sleep.get("end") or 0) == s_end
            ):
                sleep_info = stored_sleep
            else:
                sleep_info = {"date": _sday, "start": s_start, "end": s_end}
            recent_woke_up = False
        else:
            stored_sleep = state.get("sleep") if isinstance(state.get("sleep"), dict) else None
            sleep_info = stored_sleep if stored_sleep else None
            sleep_end = float(sleep_info.get("end") or 0) if sleep_info else 0.0
            recent_woke_up = bool(sleep_end > 0 and 0 <= now - sleep_end <= 2700)
        recent = [e for e in state.get("experiences", [])
                  if e.get("kind") in ("activity", "diary", "dream")]
        recent.sort(key=lambda e: e.get("created_at", 0), reverse=True)
        raw_notes = state.get("life_notes") or []
        if scope is None:
            visible_notes = list(reversed(raw_notes))[:10]
        else:
            norm_scope = str(scope or "").strip()[:64]
            visible_notes = [
                n for n in reversed(raw_notes)
                if isinstance(n, Mapping) and (
                    (n.get("source") == "manual" and not n.get("scope"))
                    or (norm_scope and n.get("scope") == norm_scope)
                )
            ][:10]
        return {
            "enabled": bool(cfg.get("enabled")),
            "mood_tone_enabled": bool(cfg.get("mood_tone_enabled", True)),
            "env_awareness_enabled": bool(cfg.get("env_awareness_enabled", True)),
            "timezone": str(cfg.get("timezone") or "Asia/Shanghai"),
            "available": True,
            "corrupt": False,
            "account": account,
            "role_id": role_id,
            "revision": self._effective_revision(account, role_id, state),
            "state_version": int(state.get("state_version") or 0),
            "epoch": int(state.get("epoch") or 0),
            "last_advance": state.get("last_advance") or 0,
            "budget_unknown": float(state.get("budget_unknown_until") or 0) > now,
            "budget_unknown_until": float(state.get("budget_unknown_until") or 0),
            "recovered_from_corrupt": bool(state.get("recovered_from_corrupt")),
            "corrupt_backup": state.get("corrupt_backup") or "",
            "date": today,
            "sleeping": sleeping,
            "is_free": bool(not sleeping and current is None),
            "recent_woke_up": recent_woke_up,
            "sleep": sleep_info,
            "current_activity": current,
            "plan": plan,
            "mood": state.get("mood") or {},
            "life_notes": visible_notes,
            "recent": [{"id": e.get("id"), "kind": e.get("kind"), "date": e.get("date"),
                        "text": e.get("text", ""), "dream": bool(e.get("dream"))}
                       for e in recent[:5]],
            "next_node": self._next_node(state, cfg, clock, now),
        }

    def _next_node(self, state: dict[str, Any], cfg: dict[str, Any], clock: _Clock,
                   now: float) -> dict[str, Any] | None:
        candidates: list[tuple[float, str]] = []
        today = clock.date_str(now)
        plan = (state.get("plans") or {}).get(today) or {}
        for item in plan.get("items", []):
            if item.get("status") == "planned" and float(item.get("start") or 0) > now:
                candidates.append((float(item["start"]), f"活动：{item.get('title')}"))
        if cfg.get("sleep_enabled"):
            for day, start, end in self._sleep_segments(cfg, clock, now, include_tomorrow=True):
                if start > now:
                    candidates.append((start, "准备睡觉"))
                elif start <= now < end:
                    return {"at": end, "label": "起床"}
        if cfg.get("diary_enabled"):
            diary_at = clock.hhmm(today, cfg["diary_time"])
            if diary_at > now:
                candidates.append((diary_at, "写日记"))
        if not candidates:
            return None
        at, label = min(candidates)
        return {"at": at, "label": label}

    def status(self, account: str, role_id: str, now: float | None = None) -> dict[str, Any]:
        now = self._now(now)
        data = self.snapshot(account, role_id, now)
        state, error = self.store.try_load(account, role_id)
        if state is None:
            data.update({"usage": self.usage(account, role_id, now), "queue": self._queue_size,
                         "jobs": [], "metrics": {}, "config": self._role_cfg(account, role_id) or {},
                         "storage_path": str(self.store.state_path(account, role_id)),
                         "corrupt": True, "error": error or "状态文件不可读"})
            return data
        jobs = list((state.get("jobs") or {}).values())
        jobs.sort(key=lambda j: j.get("created_at", 0), reverse=True)
        cfg = self._role_cfg(account, role_id) or {}
        data.update({
            "usage": self.usage(account, role_id, now),
            "queue": self._queue_size,
            "jobs": [self._public_job(j) for j in jobs[:20]],
            "metrics": state.get("metrics") or {},
            "config": copy.deepcopy(cfg),
            "storage_path": str(self.store.state_path(account, role_id)),
            "state_version": int(state.get("state_version") or 0),
            "last_advance": state.get("last_advance") or 0,
            "budget_unknown": float(state.get("budget_unknown_until") or 0) > now,
            "budget_unknown_until": float(state.get("budget_unknown_until") or 0),
            "recovered_from_corrupt": bool(state.get("recovered_from_corrupt")),
            "corrupt_backup": state.get("corrupt_backup") or "",
        })
        return data

    @staticmethod
    def _public_job(job: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": job.get("id"), "kind": job.get("kind"), "status": job.get("status"),
                "created_at": job.get("created_at"), "finished_at": job.get("finished_at"),
                "error": job.get("error", ""), "attempts": job.get("attempts", 0),
                "request_id": job.get("request_id", ""),
                "retry_of": job.get("retry_of", "")}

    def list_records(self, account: str, role_id: str, kind: str, *,
                     page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = min(50, max(1, int(page_size or 20)))
        state, error = self.store.try_load(account, role_id)
        if state is None:
            return {"items": [], "page": page, "page_size": page_size, "total": 0,
                    "pages": 1, "corrupt": True, "error": error or "状态文件不可读"}
        if kind == "experiences":
            records = list(state.get("experiences") or [])
            records.sort(key=lambda e: e.get("created_at", 0), reverse=True)
        elif kind == "plans":
            records = []
            for day, plan in sorted((state.get("plans") or {}).items(), reverse=True):
                for item in plan.get("items", []):
                    records.append(dict(item, date=day))
        elif kind == "jobs":
            records = list((state.get("jobs") or {}).values())
            records.sort(key=lambda j: j.get("created_at", 0), reverse=True)
            records = [self._public_job(j) for j in records]
        elif kind == "sleep":
            sleep = state.get("sleep")
            records = [sleep] if isinstance(sleep, dict) else []
        else:
            raise ValueError("unknown record kind")
        total = len(records)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, pages))
        start = (page - 1) * page_size
        return {"items": records[start:start + page_size], "page": page,
                "page_size": page_size, "total": total,
                "pages": pages}

    def usage(self, account: str, role_id: str, now: float | None = None) -> dict[str, Any]:
        now = self._now(now)
        state, error = self.store.try_load(account, role_id)
        account_limit = int(self._account_limit()) if callable(self._account_limit) else None
        if state is None:
            # 损坏时不能把“看不见的调用”显示成 0。
            return {"role_24h": None, "role_limit": None,
                    "account_limit": account_limit,
                    "account_id": self._account or "", "corrupt": True,
                    "error": error or "状态文件不可读"}
        calls = [t for t in (state.get("budget") or {}).get("calls", []) if now - t < 86400]
        unknown_until = float(state.get("budget_unknown_until") or 0)
        return {"role_24h": len(calls), "role_limit": None,
                "account_limit": account_limit, "account_id": self._account or "",
                "corrupt": False, "budget_unknown": unknown_until > now,
                "budget_unknown_until": unknown_until}

    def is_sleeping(self, account: str, role_id: str, now: float | None = None) -> bool:
        cfg = self._role_cfg(account, role_id)
        if not cfg or not cfg.get("enabled"):
            return False
        now = self._now(now)
        return self._current_sleep(cfg, _Clock(cfg["timezone"]), now) is not None

    def candidates(self, account: str, role_id: str, *, is_group: bool, chat_id: str,
                   now: float | None = None, limit: int = 5) -> list[dict[str, Any]]:
        now = self._now(now)
        cfg = self._role_cfg(account, role_id)
        if not cfg or not cfg.get("enabled"):
            return []  # 功能关闭时不能影响任何主动判定
        state, _error = self.store.try_load(account, role_id)
        if state is None:
            return []
        attempts = ((state.get("deliveries") or {}).get(_delivery_key(is_group, chat_id), {})
                    .get("attempts") or {})
        items = []
        # 梦也是可聊的经历，但保留 kind/dream 标记，绝不能当成现实事实。
        experiences = [e for e in state.get("experiences", [])
                       if e.get("kind") in ("activity", "diary", "dream") and e.get("text")]
        experiences.sort(key=lambda e: e.get("created_at", 0), reverse=True)
        for exp in experiences:
            if exp.get("id") in attempts:
                continue
            items.append({"item_id": exp.get("id"), "kind": exp.get("kind"),
                          "date": exp.get("date"), "text": exp.get("text", ""),
                          "created_at": exp.get("created_at", 0),
                          "dream": exp.get("kind") == "dream",
                          "origin": "role_simulation"})
            if len(items) >= limit:
                break
        return items

    def begin_delivery(self, account: str, role_id: str, *, is_group: bool, chat_id: str,
                       item_ids: Sequence[str], request_id: str = "",
                       expected_revision: str = "",
                       expected_epoch: int | None = None,
                       expected_state_version: int | None = None
                       ) -> dict[str, Any] | None:
        """发送前登记尝试，返回不可伪造的 ticket。

        - 版本校验在 store 锁内 CAS；检查后清空/并发写不会让旧请求复活。
        - 幂等键 = request_id + payload hash + 目标；同键返回同 ticket，
          同 request_id 但 payload 不同会被拒绝（不能拿旧 ticket 发新内容）。
        - ticket 只能由本模块生成，`finish_delivery` 只认 ticket。
        """
        account = validate_account(account)
        key = _delivery_key(is_group, chat_id)
        wanted = [str(x) for x in item_ids if str(x)][:MAX_DELIVERY_ITEMS]
        if not wanted:
            return None
        request_id = str(request_id or "").strip()[:64]
        if (not request_id or not str(expected_revision or "")
                or expected_state_version is None):
            return None
        payload_hash = _payload_hash("delivery", key, wanted)
        # 幂等重放：同 request_id + 同 payload 已经提交过时，即使页面持有的
        # state_version 已过期，也返回既有 ticket（不能重复占位或改写内容）。
        pre_state, _err = self.store.try_load(account, role_id)
        if pre_state is not None:
            for ticket, receipt in (pre_state.get("receipts") or {}).items():
                if (isinstance(receipt, dict)
                        and receipt.get("request_id") == request_id
                        and receipt.get("payload_hash") == payload_hash):
                    return {"ticket": ticket,
                            "items": list(receipt.get("items") or []),
                            "target": key,
                            "state_version": int(pre_state.get("state_version") or 0),
                            "epoch": int(pre_state.get("epoch") or 0)}
        effective_revision = ""
        if pre_state is not None:
            effective_revision = self._effective_revision(account, role_id, pre_state)
        if effective_revision and str(expected_revision) != effective_revision:
            return None
        expect = self._store_expect(account, role_id, expected_epoch,
                                    expected_state_version,
                                    effective_revision or expected_revision)
        result: dict[str, Any] = {}
        status_box = {"code": ""}

        def mutate(state: dict[str, Any]) -> None:
            receipts = state.setdefault("receipts", {})
            for ticket, receipt in receipts.items():
                if not isinstance(receipt, dict) or receipt.get("request_id") != request_id:
                    continue
                if receipt.get("payload_hash") == payload_hash:
                    result["ticket"] = ticket
                    result["items"] = list(receipt.get("items") or [])
                    result["target"] = key
                    result["state_version"] = int(state.get("state_version") or 0)
                    result["epoch"] = int(state.get("epoch") or 0)
                    return
                # 同一个 request_id 被拿去登记不同内容：拒绝，避免旧票复用。
                status_box["code"] = "payload_mismatch"
                return
            existing_ids = {
                str(exp.get("id") or "")
                for exp in (state.get("experiences") or [])
                if isinstance(exp, Mapping) and exp.get("id")
            }
            deliveries = state.setdefault("deliveries", {})
            entry = deliveries.setdefault(key, {"item_ids": [], "attempts": {}})
            attempts = entry.setdefault("attempts", {})
            claimed = []
            for item_id in wanted:
                if item_id not in existing_ids or item_id in attempts:
                    continue
                attempts[item_id] = "unknown"  # 在途/未知：不自动重发
                claimed.append(item_id)
            entry["item_ids"] = list(dict.fromkeys(entry.get("item_ids", []) + claimed))[
                -MAX_DELIVERY_ITEMS:]
            if len(deliveries) > MAX_DELIVERY_TARGETS:
                for stale_key in list(deliveries)[:-MAX_DELIVERY_TARGETS]:
                    deliveries.pop(stale_key, None)
            if not claimed:
                result["items"] = []
                result["ticket"] = ""
                result["target"] = key
                return
            ticket = _ticket_id(account, role_id, key, claimed, request_id,
                                int(state.get("state_version") or 0))
            receipts[ticket] = {
                "account": account, "target": key, "items": claimed,
                "status": "pending", "epoch": int(state.get("epoch") or 0),
                "state_version": int(state.get("state_version") or 0),
                "request_id": request_id, "payload_hash": payload_hash,
            }
            if len(receipts) > MAX_DELIVERY_TICKETS:
                for stale_ticket in list(receipts)[:-MAX_DELIVERY_TICKETS]:
                    receipts.pop(stale_ticket, None)
            result["ticket"] = ticket
            result["items"] = claimed
            result["target"] = key

        try:
            saved = self.store.update(account, role_id, mutate, expect=expect)
        except RoleLifeConflict:
            return None
        except Exception as exc:
            self._log(f"[RoleLife] begin_delivery failed: {exc}")
            return None
        if status_box["code"]:
            return None
        return {
            "ticket": result.get("ticket", ""),
            "items": result.get("items", []),
            "target": key,
            "state_version": int(saved.get("state_version") or result.get("state_version") or 0),
            "epoch": int(saved.get("epoch") or result.get("epoch") or 0),
        }

    def finish_delivery(self, account: str, role_id: str, ticket: str, status: str) -> bool:
        """按 ticket 结束投递；重复回执幂等，旧 epoch/别的账号不能完成。"""
        if status not in ("sent", "partial", "unknown", "failed"):
            raise ValueError("invalid delivery status")
        account = validate_account(account)
        ticket = str(ticket or "").strip()
        if not ticket:
            return False
        done: list[bool] = [False]

        def mutate(state: dict[str, Any]) -> None:
            receipt = (state.get("receipts") or {}).get(ticket)
            if not isinstance(receipt, dict):
                return
            if receipt.get("account") != account:
                return  # 别的账号不能完成本账号 ticket
            if int(receipt.get("epoch") or 0) != int(state.get("epoch") or 0):
                return  # 清空后旧 ticket 不能复活数据
            previous = receipt.get("status")
            if previous == "sent":
                done[0] = True  # 已送达，忽略降级回执
                return
            receipt["status"] = status
            key = str(receipt.get("target") or "")
            entry = (state.setdefault("deliveries", {}).get(key)
                     or {"item_ids": [], "attempts": {}})
            attempts = entry.setdefault("attempts", {})
            for item_id in receipt.get("items") or []:
                if attempts.get(item_id) == "sent" and status != "sent":
                    continue
                attempts[item_id] = status
            state.setdefault("deliveries", {})[key] = entry
            done[0] = True

        try:
            self.store.update(account, role_id, mutate)
        except Exception as exc:
            self._log(f"[RoleLife] finish_delivery failed: {exc}")
            return False
        return done[0]

    def clear_session_shares(self, account: str, role_id: str, *, is_group: bool,
                             chat_id: str) -> None:
        """普通 /reset 只清该会话分享引用；不影响其他会话或角色人生。"""
        key = _delivery_key(is_group, chat_id)

        def mutate(state: dict[str, Any]) -> None:
            (state.get("deliveries") or {}).pop(key, None)
            receipts = state.get("receipts") or {}
            for ticket in [t for t, r in receipts.items()
                           if isinstance(r, dict) and r.get("target") == key]:
                receipts.pop(ticket, None)

        self.store.update(account, role_id, mutate)

    def clear_role(self, account: str, role_id: str, *, request_id: str = "",
                   expected_revision: str = "",
                   expected_epoch: int | None = None,
                   expected_state_version: int | None = None,
                   allow_corrupt: bool = False) -> bool:
        """清空此角色生活（二次确认由 WebUI 负责）。

        - 正常状态：锁内 CAS 校验 revision/epoch/state_version，缺省一律拒绝。
        - 清空动作与 clear 的 `op_requests` 幂等记录在**同一次 store.update**
          事务内完成；不存在第二次补写，半途失败不会留下“清了但没记账”。
        - request_id 幂等：同 request_id + 同 payload 返回既有成功，不再写盘、
          不再 bump epoch；同 request_id 但 payload 不同拒绝。
        - 损坏状态：只有显式 `allow_corrupt=True` 才恢复；store 会先备份原字节，
          能独立校验原 budget 就保留，否则封住本角色 24h 额度。
        - 先成功落盘再清内存队列；写盘失败不会先改内存。
        """
        account = validate_account(account)
        request_id = str(request_id or "").strip()[:64]
        if not request_id:
            return False
        payload_hash = _payload_hash("clear", account, role_id)
        op_request = {"op": "clear", "request_id": request_id,
                      "payload_hash": payload_hash, "result": "ok"}
        state, error = self.store.try_load(account, role_id)
        if state is None:
            if not allow_corrupt:
                return False
            try:
                self.store.clear(account, role_id, reason=f"clear_corrupt:{request_id}",
                                 request_id=request_id, allow_corrupt=True,
                                 op_request=op_request)
            except Exception as exc:
                self._log(f"[RoleLife] clear_role corrupt failed: {exc}")
                return False
            with self._queue_lock:
                self._drop_role_queue_locked(account, role_id)
            return True
        if (expected_epoch is None or not str(expected_revision or "")
                or expected_state_version is None):
            return False
        effective_revision = self._effective_revision(account, role_id, state)
        if effective_revision and str(expected_revision) != effective_revision:
            return False
        for entry in (state.get("op_requests") or []):
            if (isinstance(entry, dict) and entry.get("op") == "clear"
                    and entry.get("request_id") == request_id):
                if entry.get("payload_hash") != payload_hash:
                    return False
                with self._queue_lock:
                    self._drop_role_queue_locked(account, role_id)
                return True
        expect = self._store_expect(account, role_id, expected_epoch,
                                    expected_state_version, expected_revision)
        try:
            self.store.clear(account, role_id, reason=f"clear:{request_id}",
                             request_id=request_id, expect=expect,
                             op_request=op_request)
        except RoleLifeConflict:
            return False
        except Exception as exc:
            self._log(f"[RoleLife] clear_role failed: {exc}")
            return False
        with self._queue_lock:
            self._drop_role_queue_locked(account, role_id)
        return True

    def _drop_role_queue_locked(self, account: str, role_id: str) -> None:
        key = (account, role_id)
        queue = self._pending.pop(key, None)
        if queue:
            for job_id in queue:
                self._queued_ids.discard((account, role_id, job_id))
                self._queue_size = max(0, self._queue_size - 1)
        if key in self._rotation_set:
            self._rotation_set.discard(key)
            try:
                self._role_rotation.remove(key)
            except ValueError:
                pass

    # -- 手动作业 ---------------------------------------------------------
    def enqueue(self, account: str, role_id: str, kind: str, *,
                request_id: str = "", args: Mapping[str, Any] | None = None,
                now: float | None = None, expected_revision: str = "",
                expected_epoch: int | None = None,
                expected_state_version: int | None = None) -> str | None:
        if kind not in JOB_KINDS:
            raise ValueError("unknown job kind")
        account = validate_account(account)
        cfg = self._role_cfg(account, role_id)
        if not cfg or not cfg.get("enabled"):
            return None
        if self._stopping.is_set() or self._account != account:
            return None
        # 外部写操作必须显式带版本与幂等 ID；不能因默认空值而静默跳过校验。
        if (expected_epoch is None or not str(expected_revision or "")
                or not str(request_id or "") or expected_state_version is None):
            return None
        now = self._now(now)
        persona = self._persona(account, role_id)
        if persona is None:
            return None
        current, _error = self.store.try_load(account, role_id)
        if current is None:
            return None
        # 首次操作时持久化 revision 还是空串：用当前 persona revision 参与 CAS。
        effective_revision = self._effective_revision(account, role_id, current)
        if effective_revision and str(expected_revision) != effective_revision:
            return None
        request_id = str(request_id or "").strip()[:64]
        job_box: list[str] = []
        outcome = {"code": ""}
        clock = _Clock(cfg["timezone"])
        today = clock.date_str(now)
        payload = dict(args or {})
        hash_payload = payload
        if kind == "plan":
            payload.setdefault("date", today)
            payload["replace"] = True
            hash_payload = dict(payload)
            payload["current_time"] = clock.local(now).strftime("%H:%M")
            today_ctx = _today_plan_context(current, str(payload["date"]), clock)
            if today_ctx:
                payload["plan_context"] = today_ctx
            notes = _recent_life_notes(current, clock, str(payload["date"]))
            if notes:
                payload["life_notes"] = notes
        elif kind == "activity":
            if not payload.get("item"):
                return None
        elif kind == "diary":
            target_day = str(payload.get("date") or today).strip() or today
            payload["date"] = target_day
            if any(
                e.get("kind") == "diary" and e.get("date") == target_day
                for e in (current.get("experiences") or [])
            ):
                raise ValueError("当天日记已存在")
            payload["material"] = self._diary_material(current, target_day)
            if not payload["material"]:
                return None
        elif kind == "dream":
            if not payload.get("sleep_start"):
                sleep = current.get("sleep") or {}
                if not sleep:
                    return None
                payload["sleep_start"] = sleep.get("start")
                payload["sleep_end"] = sleep.get("end")
            dream_id = f"dream-{int(float(payload.get('sleep_start') or 0))}"
            if any(
                e.get("kind") == "dream" and e.get("id") == dream_id
                for e in (current.get("experiences") or [])
            ):
                raise ValueError("本次睡眠的梦境已生成")
            payload["material"] = self._recent_material(current, limit=6)
        elif kind == "maintenance":
            payload["material"] = self._recent_material(current)
        payload_hash = _payload_hash("enqueue", kind, hash_payload)
        for entry in (current.get("op_requests") or []):
            if (isinstance(entry, dict) and entry.get("op") == f"enqueue:{kind}"
                    and entry.get("request_id") == request_id):
                if entry.get("payload_hash") != payload_hash:
                    return None
                existing = str(entry.get("result") or "")
                if existing:
                    return existing
                return None

        def mutate(state: dict[str, Any]) -> None:
            jobs = state.setdefault("jobs", {})
            ops = state.setdefault("op_requests", [])
            for entry in ops:
                if (isinstance(entry, dict) and entry.get("op") == f"enqueue:{kind}"
                        and entry.get("request_id") == request_id):
                    if entry.get("payload_hash") != payload_hash:
                        outcome["code"] = "payload_mismatch"
                        return
                    job_box.append(str(entry.get("result") or ""))
                    return
            state["revision"] = persona.revision
            state["config_revision"] = config_revision(cfg)
            if not self._evict_jobs_for_capacity(state):
                outcome["code"] = "capacity"
                return
            job_id = _job_id(account, role_id, kind, request_id, int(now))
            jobs[job_id] = {
                "id": job_id, "kind": kind, "status": "pending",
                "created_at": now, "started_at": 0, "finished_at": 0,
                "args": copy.deepcopy(payload), "error": "", "result_id": "",
                "revision": persona.revision,
                "config_revision": config_revision(cfg),
                "epoch": int(state.get("epoch") or 0),
                "generation": int(self._generation or 0), "attempts": 0,
                "dedupe_key": f"manual:{request_id}", "request_id": request_id,
            }
            state.setdefault("job_order", []).append(job_id)
            ops.append({"op": f"enqueue:{kind}", "request_id": request_id,
                        "payload_hash": payload_hash, "result": job_id,
                        "epoch": int(state.get("epoch") or 0)})
            state["op_requests"] = ops[-MAX_OP_REQUESTS:]
            job_box.append(job_id)

        try:
            saved = self.store.update(
                account, role_id, mutate,
                expect=self._store_expect(account, role_id, expected_epoch,
                                          expected_state_version, expected_revision))
        except RoleLifeConflict:
            return None
        except Exception:
            return None
        if outcome["code"]:
            return None
        job_id = job_box[0] if job_box else ""
        if not job_id or job_id not in (saved.get("jobs") or {}):
            return None
        self._ensure_worker_threadsafe()
        self._push_jobs([(account, role_id, job_id)])
        return job_id

    def retry_job(self, account: str, role_id: str, job_id: str, *,
                  request_id: str = "", expected_revision: str = "",
                  expected_epoch: int | None = None,
                  expected_state_version: int | None = None) -> str | None:
        """人工重试 failed/unknown 作业：创建新 job ID，原作业与预算记录保留。

        旧作业保持原状并记 retried_by；新作业使用当前 revision/config，
        并重新走一次 reserve（这是一次新的真实请求）。
        """
        account = validate_account(account)
        if self._stopping.is_set() or self._account != account:
            return None
        cfg = self._role_cfg(account, role_id)
        if not cfg or not cfg.get("enabled"):
            return None
        if (expected_epoch is None or not str(expected_revision or "")
                or not str(request_id or "") or expected_state_version is None):
            return None
        persona = self._persona(account, role_id)
        if persona is None:
            return None
        current, _error = self.store.try_load(account, role_id)
        if current is None:
            return None
        effective_revision = self._effective_revision(account, role_id, current)
        if effective_revision and str(expected_revision) != effective_revision:
            return None
        request_id = str(request_id or "").strip()[:64]
        now = self._now()
        box: list[str] = []
        outcome = {"code": ""}
        payload_hash = _payload_hash("retry", job_id)
        for entry in (current.get("op_requests") or []):
            if (isinstance(entry, dict) and entry.get("op") == "retry"
                    and entry.get("request_id") == request_id):
                if entry.get("payload_hash") != payload_hash:
                    return None
                existing = str(entry.get("result") or "")
                return existing or None

        def mutate(state: dict[str, Any]) -> None:
            jobs = state.setdefault("jobs", {})
            ops = state.setdefault("op_requests", [])
            for entry in ops:
                if (isinstance(entry, dict) and entry.get("op") == "retry"
                        and entry.get("request_id") == request_id):
                    if entry.get("payload_hash") != payload_hash:
                        outcome["code"] = "payload_mismatch"
                        return
                    box.append(str(entry.get("result") or ""))
                    return
            original = jobs.get(job_id)
            if not isinstance(original, dict) or original.get("status") not in ("failed", "unknown"):
                outcome["code"] = "not_retryable"
                return
            if not self._evict_jobs_for_capacity(state):
                outcome["code"] = "capacity"
                return
            original["retried_at"] = now
            original["retried_by"] = request_id
            new_id = _job_id(account, role_id, str(original.get("kind") or ""),
                             f"retry:{request_id}", int(now))
            clock = _Clock(cfg["timezone"])
            today = clock.date_str(now)
            orig_kind = str(original.get("kind") or "")
            new_args = copy.deepcopy(original.get("args") or {})
            if orig_kind == "plan":
                target_day = str(new_args.get("date") or today)
                fresh_notes = _recent_life_notes(state, clock, target_day)
                if fresh_notes:
                    new_args["life_notes"] = fresh_notes
                else:
                    new_args.pop("life_notes", None)
            elif orig_kind == "diary":
                target_day = str(new_args.get("date") or today)
                new_args["material"] = self._diary_material(state, target_day)
            elif orig_kind == "activity":
                new_args["manual"] = True
            state["revision"] = persona.revision
            state["config_revision"] = config_revision(cfg)
            jobs[new_id] = {
                "id": new_id, "kind": original.get("kind"), "status": "pending",
                "created_at": now, "started_at": 0, "finished_at": 0,
                "args": new_args, "error": "",
                "result_id": "", "revision": persona.revision,
                "config_revision": config_revision(cfg),
                "epoch": int(state.get("epoch") or 0),
                "generation": int(self._generation or 0), "attempts": 0,
                "dedupe_key": f"retry:{job_id}:{request_id}",
                "request_id": request_id, "retry_of": job_id,
            }
            state.setdefault("job_order", []).append(new_id)
            ops.append({"op": "retry", "request_id": request_id,
                        "payload_hash": payload_hash, "result": new_id,
                        "epoch": int(state.get("epoch") or 0)})
            state["op_requests"] = ops[-MAX_OP_REQUESTS:]
            box.append(new_id)

        try:
            self.store.update(
                account, role_id, mutate,
                expect=self._store_expect(account, role_id, expected_epoch,
                                          expected_state_version, expected_revision))
        except RoleLifeConflict:
            return None
        except Exception:
            return None
        if outcome["code"] or not box or not box[0]:
            return None
        self._ensure_worker_threadsafe()
        self._push_jobs([(account, role_id, box[0])])
        return box[0]

    def regenerate_plan(self, account: str, role_id: str, *, request_id: str = "",
                        expected_revision: str = "",
                        expected_epoch: int | None = None,
                        expected_state_version: int | None = None) -> str | None:
        return self.enqueue(account, role_id, "plan", request_id=request_id,
                            expected_revision=expected_revision,
                            expected_epoch=expected_epoch,
                            expected_state_version=expected_state_version)

    def _prepare_cas_write(
        self, account: str, role_id: str, *,
        expected_revision: str = "",
        expected_epoch: int | None = None,
        expected_state_version: int | None = None,
    ) -> tuple[dict[str, Any], Persona, dict[str, Any], dict[str, Any]]:
        account = validate_account(account)
        if self._stopping.is_set() or self._account != account:
            raise ValueError("当前账号不在线或未绑定")
        cfg = self._role_cfg(account, role_id) or copy.deepcopy(DEFAULT_ROLE_CONFIG)
        persona = self._persona(account, role_id)
        if persona is None:
            raise ValueError("人格预设不存在")
        current, err = self.store.try_load(account, role_id)
        if current is None:
            raise RoleLifeCorruptError(err or "状态文件不可读")
        effective_revision = self._effective_revision(account, role_id, current)
        if expected_revision and effective_revision and str(expected_revision) != effective_revision:
            raise RoleLifeConflict("revision mismatch")
        expect = self._store_expect(account, role_id, expected_epoch,
                                    expected_state_version, expected_revision)
        return cfg, persona, current, expect

    def _replay_op_hit(
        self, state: Mapping[str, Any], op: str, request_id: str, payload_hash: str,
        *, expected_epoch: int | None = None,
    ) -> str | None:
        if not request_id:
            return None
        if expected_epoch is not None and int(expected_epoch) != int(state.get("epoch") or 0):
            return None
        return _find_op_request(state, op, request_id, payload_hash)

    def _commit_cas_idempotent(
        self, account: str, role_id: str, *,
        op: str, request_id: str, payload_hash: str,
        current: dict[str, Any], expect: dict[str, Any],
        expected_epoch: int | None,
        resolve_replay: Callable[[dict[str, Any], str], Any],
        mutate_fn: Callable[[dict[str, Any]], tuple[str, Any]],
    ) -> Any:
        req_id = str(request_id or "").strip()[:64]
        hit = self._replay_op_hit(current, op, req_id, payload_hash, expected_epoch=expected_epoch)
        if hit is not None:
            return resolve_replay(current, hit)
        out_box: list[Any] = []

        def wrapped_mutate(state: dict[str, Any]) -> None:
            replay_id = _find_op_request(state, op, req_id, payload_hash) if req_id else None
            if replay_id is not None:
                out_box.append(resolve_replay(state, replay_id))
                return
            res_id, val = mutate_fn(state)
            if req_id and res_id:
                _record_op_request(state, op, req_id, payload_hash, res_id)
            out_box.append(val)

        try:
            self.store.update(account, role_id, wrapped_mutate, expect=expect or None)
        except RoleLifeConflict:
            if req_id:
                after, _err = self.store.try_load(account, role_id)
                if after is not None:
                    replay_id = self._replay_op_hit(
                        after, op, req_id, payload_hash, expected_epoch=expected_epoch,
                    )
                    if replay_id is not None:
                        return resolve_replay(after, replay_id)
            raise
        return out_box[0]

    def upsert_plan_item(
        self, account: str, role_id: str, *,
        item_id: str = "", title: str, purpose: str = "",
        start_hhmm: str, end_hhmm: str, date: str = "",
        request_id: str = "", expected_revision: str = "",
        expected_epoch: int | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        cfg, persona, current, expect = self._prepare_cas_write(
            account, role_id, expected_revision=expected_revision,
            expected_epoch=expected_epoch, expected_state_version=expected_state_version)
        clean_title = str(title or "").strip()
        clean_purpose = str(purpose or "").strip()
        if not clean_title or len(clean_title) > 80:
            raise ValueError("活动名称需在 1～80 字之间")
        if len(clean_purpose) > 200:
            raise ValueError("活动说明不能超过 200 字")
        start_str = _parse_hhmm(start_hhmm, "start")
        end_str = _parse_hhmm(end_hhmm, "end")
        clock = _Clock(cfg["timezone"])
        now = self._now()
        target_day = str(date or clock.date_str(now)).strip()
        if not _valid_date(target_day):
            raise ValueError("日期格式无效")
        start_ts = clock.hhmm(target_day, start_str)
        end_ts = clock.hhmm(target_day, end_str)
        if end_ts <= start_ts:
            raise ValueError("结束时间必须晚于开始时间")
        item_id = str(item_id or "").strip()
        op = "upsert_plan_item"
        p_hash = _payload_hash(op, item_id, clean_title, clean_purpose, start_str, end_str, target_day)

        def resolve_replay(state: dict[str, Any], hit_id: str) -> dict[str, Any]:
            for p in (state.get("plans") or {}).values():
                if isinstance(p, dict):
                    for candidate in p.get("items", []):
                        if candidate.get("id") == hit_id:
                            return copy.deepcopy(candidate)
            return {"id": hit_id, "title": clean_title, "purpose": clean_purpose, "status": "planned"}

        def mutate_fn(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            state["revision"] = persona.revision
            plans = state.setdefault("plans", {})
            if item_id:
                target_day_key = ""
                target_plan = None
                target_item = None
                for day_key, p in plans.items():
                    if isinstance(p, dict):
                        for candidate in p.get("items", []):
                            if candidate.get("id") == item_id:
                                target_day_key, target_plan, target_item = day_key, p, candidate
                                break
                    if target_item is not None:
                        break
                if target_item is None or target_plan is None:
                    raise ValueError("未找到该日程条目")
                if target_item.get("status") != "planned":
                    raise ValueError("只能修改未开始的活动")
                plan_day = target_day_key if _valid_date(target_day_key) else str(target_plan.get("plan_id") or "")[:10]
                if not _valid_date(plan_day):
                    plan_day = clock.date_str(float(target_item.get("start") or now))
                s_ts = clock.hhmm(plan_day, start_str)
                e_ts = clock.hhmm(plan_day, end_str)
                for other in target_plan.get("items", []):
                    if other.get("id") == item_id or other.get("status") in ("skipped", "interrupted"):
                        continue
                    if s_ts < float(other.get("end") or 0) and e_ts > float(other.get("start") or 0):
                        raise ValueError("调整后的时间与其他日程重叠")
                target_item["title"] = clean_title
                target_item["purpose"] = clean_purpose
                target_item["start"] = s_ts
                target_item["end"] = e_ts
                target_plan["items"].sort(key=lambda x: float(x.get("start") or 0))
                return item_id, copy.deepcopy(target_item)

            plan = plans.get(target_day)
            if not isinstance(plan, dict):
                serial = int(state.get("plan_serial") or 0) + 1
                state["plan_serial"] = serial
                plan = {"plan_id": f"{target_day}-p{serial}", "serial": serial,
                        "generated_at": now, "items": []}
                plans[target_day] = plan
            items = plan.setdefault("items", [])
            if len(items) >= MAX_PLAN_ITEMS:
                raise ValueError(f"当天日程最多保留 {MAX_PLAN_ITEMS} 项")
            for other in items:
                if other.get("status") in ("skipped", "interrupted"):
                    continue
                if start_ts < float(other.get("end") or 0) and end_ts > float(other.get("start") or 0):
                    raise ValueError("时间与已有日程重叠")
            serial = int(plan.get("serial") or state.get("plan_serial") or 1)
            existing_ids = {str(i.get("id") or "") for i in items}
            idx = len(items) + 1
            new_id = f"{target_day}-p{serial}-m{idx}"
            while new_id in existing_ids:
                idx += 1
                new_id = f"{target_day}-p{serial}-m{idx}"
            created = {
                "id": new_id, "title": clean_title, "purpose": clean_purpose,
                "start": start_ts, "end": end_ts, "status": "planned",
                "origin": "role_simulation", "started_at": 0, "ended_at": 0,
                "experience_id": "",
            }
            items.append(created)
            items.sort(key=lambda x: float(x.get("start") or 0))
            return new_id, copy.deepcopy(created)

        return self._commit_cas_idempotent(
            account, role_id, op=op, request_id=request_id, payload_hash=p_hash,
            current=current, expect=expect, expected_epoch=expected_epoch,
            resolve_replay=resolve_replay, mutate_fn=mutate_fn,
        )

    def delete_plan_item(
        self, account: str, role_id: str, *, item_id: str,
        request_id: str = "", expected_revision: str = "",
        expected_epoch: int | None = None,
        expected_state_version: int | None = None,
    ) -> bool:
        _cfg, persona, current, expect = self._prepare_cas_write(
            account, role_id, expected_revision=expected_revision,
            expected_epoch=expected_epoch, expected_state_version=expected_state_version)
        target_id = str(item_id or "").strip()
        if not target_id:
            raise ValueError("缺少日程条目 ID")
        op = "delete_plan_item"
        p_hash = _payload_hash(op, target_id)

        def mutate_fn(state: dict[str, Any]) -> tuple[str, bool]:
            state["revision"] = persona.revision
            for plan in (state.get("plans") or {}).values():
                if not isinstance(plan, dict):
                    continue
                items = plan.get("items") or []
                for idx, candidate in enumerate(items):
                    if candidate.get("id") == target_id:
                        if candidate.get("status") != "planned":
                            raise ValueError("只能删除未开始的活动")
                        items.pop(idx)
                        return target_id, True
            raise ValueError("未找到该日程条目")

        return bool(self._commit_cas_idempotent(
            account, role_id, op=op, request_id=request_id, payload_hash=p_hash,
            current=current, expect=expect, expected_epoch=expected_epoch,
            resolve_replay=lambda _st, _hid: True, mutate_fn=mutate_fn,
        ))

    def trigger_activity_experience(
        self, account: str, role_id: str, *, item_id: str,
        request_id: str = "", expected_revision: str = "",
        expected_epoch: int | None = None,
        expected_state_version: int | None = None,
    ) -> str | None:
        cfg, persona, current, _expect = self._prepare_cas_write(
            account, role_id, expected_revision=expected_revision,
            expected_epoch=expected_epoch, expected_state_version=expected_state_version)
        if not cfg.get("enabled"):
            raise ValueError("请先打开「角色生活总开关」并保存设置，再生成经历")
        if (not expected_revision and expected_epoch is None and expected_state_version is None):
            expected_revision = self._effective_revision(account, role_id, current) or persona.revision
            expected_epoch = int(current.get("epoch") or 0)
            expected_state_version = int(current.get("state_version") or 0)
        target_id = str(item_id or "").strip()
        if not target_id:
            raise ValueError("缺少日程条目 ID")
        clock = _Clock(cfg["timezone"])
        found_day, found_plan, found_item = "", None, None
        for day, plan in (current.get("plans") or {}).items():
            if not isinstance(plan, dict):
                continue
            for item in plan.get("items") or []:
                if item.get("id") == target_id:
                    found_day, found_plan, found_item = day, plan, item
                    break
            if found_item is not None:
                break
        if found_item is None or found_plan is None:
            raise ValueError("未找到该日程条目")
        if found_item.get("experience_id"):
            raise ValueError("该活动已有经历记录，可直接编辑")
        if found_item.get("status") not in ("planned", "active", "interrupted"):
            raise ValueError("该活动当前状态不支持生成经历")
        day_str = found_day if _valid_date(found_day) else clock.date_str(self._now())
        args = {
            "date": day_str,
            "plan_serial": int(found_plan.get("serial") or 0),
            "manual": True,
            "item": {
                "id": found_item.get("id"),
                "title": found_item.get("title"),
                "purpose": found_item.get("purpose", ""),
                "start": found_item.get("start"),
                "end": found_item.get("end"),
            },
            "plan_context": [
                {"title": i.get("title"), "purpose": i.get("purpose")}
                for i in (found_plan.get("items") or [])[:MAX_PLAN_ITEMS]
            ],
        }
        return self.enqueue(
            account, role_id, "activity", request_id=request_id, args=args,
            expected_revision=expected_revision, expected_epoch=expected_epoch,
            expected_state_version=expected_state_version,
        )

    def update_experience(
        self, account: str, role_id: str, *,
        experience_id: str = "", plan_item_id: str = "", text: str,
        request_id: str = "", expected_revision: str = "",
        expected_epoch: int | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        cfg, persona, current, expect = self._prepare_cas_write(
            account, role_id, expected_revision=expected_revision,
            expected_epoch=expected_epoch, expected_state_version=expected_state_version)
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("内容不能为空")
        exp_id = str(experience_id or "").strip()
        item_id = str(plan_item_id or "").strip()
        if not exp_id and not item_id:
            raise ValueError("缺少记录或活动 ID")
        clock = _Clock(cfg["timezone"])
        now = self._now()
        op = "update_experience"
        p_hash = _payload_hash(op, exp_id, item_id, clean_text)

        def resolve_replay(state: dict[str, Any], hit_id: str) -> dict[str, Any]:
            for exp in (state.get("experiences") or []):
                if exp.get("id") == hit_id:
                    return copy.deepcopy(exp)
            return {"id": hit_id, "text": clean_text}

        def mutate_fn(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            state["revision"] = persona.revision
            exps = state.setdefault("experiences", [])
            if exp_id:
                for exp in exps:
                    if exp.get("id") == exp_id:
                        limit = 1600 if exp.get("kind") == "diary" else 800 if exp.get("kind") == "dream" else 1200
                        exp["text"] = clean_text[:limit]
                        return exp_id, copy.deepcopy(exp)
                raise ValueError("未找到该生活记录")
            for day, plan in (state.get("plans") or {}).items():
                if not isinstance(plan, dict):
                    continue
                for item in plan.get("items") or []:
                    if item.get("id") == item_id:
                        linked_id = str(item.get("experience_id") or "")
                        if linked_id:
                            for exp in exps:
                                if exp.get("id") == linked_id:
                                    exp["text"] = clean_text[:1200]
                                    return linked_id, copy.deepcopy(exp)
                        new_exp_id = f"exp-{item_id}"
                        created = {
                            "id": new_exp_id, "kind": "activity",
                            "date": day if _valid_date(day) else clock.date_str(now),
                            "created_at": now, "text": clean_text[:1200],
                            "source_ids": [item_id], "origin": "role_simulation", "dream": False,
                        }
                        exps.append(created)
                        item["status"] = "completed"
                        item["experience_id"] = new_exp_id
                        item["ended_at"] = now
                        return new_exp_id, copy.deepcopy(created)
            raise ValueError("未找到对应的日程活动")

        return self._commit_cas_idempotent(
            account, role_id, op=op, request_id=request_id, payload_hash=p_hash,
            current=current, expect=expect, expected_epoch=expected_epoch,
            resolve_replay=resolve_replay, mutate_fn=mutate_fn,
        )

    def delete_experience(
        self, account: str, role_id: str, *, experience_id: str,
        request_id: str = "", expected_revision: str = "",
        expected_epoch: int | None = None,
        expected_state_version: int | None = None,
    ) -> bool:
        _cfg, persona, current, expect = self._prepare_cas_write(
            account, role_id, expected_revision=expected_revision,
            expected_epoch=expected_epoch, expected_state_version=expected_state_version)
        exp_id = str(experience_id or "").strip()
        if not exp_id:
            raise ValueError("缺少记录 ID")
        op = "delete_experience"
        p_hash = _payload_hash(op, exp_id)

        def mutate_fn(state: dict[str, Any]) -> tuple[str, bool]:
            state["revision"] = persona.revision
            exps = state.get("experiences") or []
            for idx, exp in enumerate(exps):
                if exp.get("id") == exp_id:
                    exps.pop(idx)
                    for plan in (state.get("plans") or {}).values():
                        if isinstance(plan, dict):
                            for item in plan.get("items") or []:
                                if item.get("experience_id") == exp_id:
                                    item["experience_id"] = ""
                    return exp_id, True
            raise ValueError("未找到该生活记录")

        return bool(self._commit_cas_idempotent(
            account, role_id, op=op, request_id=request_id, payload_hash=p_hash,
            current=current, expect=expect, expected_epoch=expected_epoch,
            resolve_replay=lambda _st, _hid: True, mutate_fn=mutate_fn,
        ))

    def add_life_note(
        self, account: str, role_id: str, text: str, *,
        source: str = "chat_feedback", strip_names: Iterable[str] = (),
        scope: str = "", request_id: str = "",
        now: float | None = None, expected_revision: str = "",
        expected_epoch: int | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any] | None:
        account = validate_account(account)
        if source != "manual":
            # 已取消聊天约定自动提取，仅保留管理员手动记录的生活备注。
            return None
        clean_scope = ""
        with self._lifecycle_lock:
            bound_account = self._account
        if self._stopping.is_set() or bound_account != account:
            raise ValueError("角色生活运行时未就绪")
        cfg = self._role_cfg(account, role_id) or copy.deepcopy(DEFAULT_ROLE_CONFIG)
        clean = sanitize_chat_life_note(text, strip_names=strip_names)
        if not clean:
            raise ValueError("备注内容不能为空或过短")
        persona = self._persona(account, role_id)
        if persona is None:
            raise ValueError("人格预设不存在")
        now = self._now(now)
        clock = _Clock(cfg["timezone"])
        today = clock.date_str(now)
        _c, _p, current, expect = self._prepare_cas_write(
            account, role_id, expected_revision=expected_revision,
            expected_epoch=expected_epoch, expected_state_version=expected_state_version)
        op = "add_life_note"
        p_hash = _payload_hash(op, clean)

        def resolve_replay(state: dict[str, Any], hit_id: str) -> dict[str, Any]:
            for n in (state.get("life_notes") or []):
                if n.get("id") == hit_id:
                    return copy.deepcopy(n)
            return {"id": hit_id, "date": today, "text": clean, "source": "manual", "scope": ""}

        def mutate_manual(state: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
            state["revision"] = persona.revision
            notes = state.setdefault("life_notes", [])
            if any(n.get("date") == today and n.get("text") == clean
                   and str(n.get("scope") or "") == clean_scope for n in notes):
                return "", None
            created = {
                "id": f"note-{int(now * 1000)}-{len(notes)}",
                "date": today,
                "text": clean,
                "created_at": now,
                "source": "manual",
                "scope": "",
            }
            notes.append(created)
            return created["id"], copy.deepcopy(created)

        return self._commit_cas_idempotent(
            account, role_id, op=op, request_id=request_id, payload_hash=p_hash,
            current=current, expect=expect, expected_epoch=expected_epoch,
            resolve_replay=resolve_replay, mutate_fn=mutate_manual,
        )

    def delete_life_note(
        self, account: str, role_id: str, note_id: str, *,
        request_id: str = "", expected_revision: str = "",
        expected_epoch: int | None = None,
        expected_state_version: int | None = None,
    ) -> bool:
        _cfg, persona, current, expect = self._prepare_cas_write(
            account, role_id, expected_revision=expected_revision,
            expected_epoch=expected_epoch, expected_state_version=expected_state_version)
        nid = str(note_id or "").strip()
        if not nid:
            raise ValueError("缺少备注 ID")
        op = "delete_life_note"
        p_hash = _payload_hash(op, nid)

        def mutate_fn(state: dict[str, Any]) -> tuple[str, bool]:
            state["revision"] = persona.revision
            notes = state.get("life_notes") or []
            for idx, note in enumerate(notes):
                if note.get("id") == nid:
                    notes.pop(idx)
                    return nid, True
            raise ValueError("未找到该生活备注")

        return bool(self._commit_cas_idempotent(
            account, role_id, op=op, request_id=request_id, payload_hash=p_hash,
            current=current, expect=expect, expected_epoch=expected_epoch,
            resolve_replay=lambda _st, _hid: True, mutate_fn=mutate_fn,
        ))


def _apply_optional_mood(state: dict[str, Any], result: Any, source_id: str, now: float) -> None:
    if not isinstance(result, Mapping):
        return
    mood = str(result.get("mood", "") or "").strip()[:40]
    energy = str(result.get("energy", "") or "").strip()[:40]
    if not mood and not energy:
        return
    prev = state.get("mood") if isinstance(state.get("mood"), dict) else {}
    final_mood = mood or str(prev.get("mood") or "") or "平稳"
    final_energy = energy or str(prev.get("energy") or "")
    state["mood"] = {
        "mood": final_mood,
        "energy": final_energy,
        "source_ids": [source_id],
        "updated_at": now,
    }


def _recent_life_notes(state: Mapping[str, Any], clock: _Clock, today: str) -> list[str]:
    yesterday = clock.add_days(today, -1)
    notes = [n for n in (state.get("life_notes") or [])
             if isinstance(n, Mapping) and n.get("date") in (today, yesterday)
             and n.get("source") == "manual" and not n.get("scope") and n.get("text")]
    return [str(n["text"])[:120] for n in notes[-4:]]


_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_MENTION_RE = re.compile(r"@\S+")
_LONG_DIGITS_RE = re.compile(r"\d{5,}")
_WS_RE = re.compile(r"\s+")


def sanitize_chat_life_note(text: str, *, strip_names: Iterable[str] = ()) -> str:
    """严格脱敏聊天小事：去除 QQ 号、链接、邮箱、@提及与具体昵称，不留真实身份。"""
    s = str(text or "").strip()
    if not s:
        return ""
    s = _CQ_RE.sub("", s)
    s = _URL_RE.sub("", s)
    s = _EMAIL_RE.sub("", s)
    s = _MENTION_RE.sub("", s)
    s = _LONG_DIGITS_RE.sub("", s)
    names = sorted({str(n or "").strip() for n in strip_names if str(n or "").strip()},
                   key=len, reverse=True)
    for name in names:
        if len(name) < 2 and not any("\u4e00" <= ch <= "\u9fff" for ch in name):
            continue
        if name in ("我", "你", "他", "她", "它", "对方", "朋友", "用户"):
            continue
        s = re.sub(re.escape(name), "朋友", s, flags=re.IGNORECASE)
    s = _WS_RE.sub(" ", s).strip(" ,，。.;；:：!！?？~～-")
    if len(s) < 4:
        return ""
    return s[:120]


def _today_plan_context(state: Mapping[str, Any], today: str, clock: _Clock) -> list[dict[str, str]]:
    """收集当天已开始/已结束的日程条目，供重排计划避让与衔接。"""
    plan = (state.get("plans") or {}).get(today)
    if not isinstance(plan, Mapping):
        return []
    out: list[dict[str, str]] = []
    for item in plan.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "")
        if status == "planned":
            continue
        title = str(item.get("title") or "").strip()[:80]
        if not title:
            continue
        start_ts = item.get("start")
        end_ts = item.get("end")
        start_str = clock.local(float(start_ts)).strftime("%H:%M") if isinstance(start_ts, (int, float)) else ""
        end_str = clock.local(float(end_ts)).strftime("%H:%M") if isinstance(end_ts, (int, float)) else ""
        out.append({"title": title, "start": start_str, "end": end_str, "status": status})
    return out[:MAX_PLAN_ITEMS]


def _valid_date(text: str) -> bool:
    try:
        _date.fromisoformat(str(text))
        return True
    except (TypeError, ValueError):
        return False


def _delivery_key(is_group: bool, chat_id: Any) -> str:
    return f"{'group' if is_group else 'private'}:{str(chat_id)}".replace(":", "|", 1)


def _payload_hash(op: str, *parts: Any) -> str:
    payload = "\u0000".join([str(op)] + [json.dumps(p, ensure_ascii=False, sort_keys=True)
                                          if not isinstance(p, str) else p
                                          for p in parts])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _find_op_request(state: Mapping[str, Any], op: str, request_id: str, payload_hash: str) -> str | None:
    if not request_id:
        return None
    cur_epoch = int(state.get("epoch") or 0)
    for entry in (state.get("op_requests") or []):
        if (isinstance(entry, Mapping) and entry.get("op") == op
                and entry.get("request_id") == request_id
                and int(entry.get("epoch") or 0) == cur_epoch):
            if entry.get("payload_hash") != payload_hash:
                raise RoleLifeConflict("request_id 与参数不匹配")
            return str(entry.get("result") or "")
    return None


def _record_op_request(state: dict[str, Any], op: str, request_id: str, payload_hash: str, result: str) -> None:
    if not request_id:
        return
    ops = state.setdefault("op_requests", [])
    ops.append({
        "op": op,
        "request_id": request_id,
        "payload_hash": payload_hash,
        "result": str(result or "")[:80],
        "epoch": int(state.get("epoch") or 0),
    })
    state["op_requests"] = ops[-MAX_OP_REQUESTS:]


def _ticket_id(account: str, role_id: str, target: str, items: Sequence[str],
               request_id: str, now: int) -> str:
    payload = "\x00".join([account, role_id, target, request_id, str(now)] + list(items))
    return "ticket-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _job_id(account: str, role_id: str, kind: str, key: str, now: int) -> str:
    digest = hashlib.sha1(
        f"{account}\x00{role_id}\x00{kind}\x00{key}\x00{now}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{kind}-{digest}"


# ---------------------------------------------------------------------------
# 独立后台调度器与单例入口：不依赖主动回复模块。
# ---------------------------------------------------------------------------


class RoleLifeScheduler:
    """角色生活独立调度器：在专属后台事件循环里推进日常时间线与作业队列。"""

    TICK_SECONDS = 30.0

    def __init__(self, engine: RoleLifeEngine, *, tick_seconds: float = TICK_SECONDS) -> None:
        self._engine = engine
        self._tick_seconds = max(0.01, float(tick_seconds))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="role-life-scheduler", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            self._stop.set()
            self._engine.request_stop()
            thread, loop, task = self._thread, self._loop, self._task
            if loop is not None and task is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        with self._lock:
            if not (thread and thread.is_alive()) and self._thread is thread:
                self._thread = None

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
            self._task = loop.create_task(self._async_loop())
            task = self._task
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._engine._log(f"[RoleLife] scheduler loop error: {exc}")
        finally:
            try:
                loop.run_until_complete(self._engine.stop())
            except Exception:
                pass
            try:
                pending = asyncio.all_tasks(loop)
                for p in pending:
                    p.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                loop.close()
                asyncio.set_event_loop(None)
                with self._lock:
                    self._loop = None
                    self._task = None

    async def _async_loop(self) -> None:
        self._engine._ensure_worker()
        while not self._stop.is_set():
            try:
                await self._engine.advance(_time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._engine._log(f"[RoleLife] scheduler advance error: {exc}")
            if self._stop.is_set():
                break
            await asyncio.sleep(self._tick_seconds)


_engine: RoleLifeEngine | None = None
_engine_lock = threading.Lock()
_scheduler: RoleLifeScheduler | None = None
_scheduler_lock = threading.Lock()


def set_engine(engine: RoleLifeEngine | None) -> None:
    global _engine
    with _engine_lock:
        _engine = engine


def get_engine() -> RoleLifeEngine | None:
    return _engine


def start_role_life_scheduler(
    engine: RoleLifeEngine | None = None,
    *,
    tick_seconds: float = RoleLifeScheduler.TICK_SECONDS,
) -> RoleLifeScheduler | None:
    """幂等启动角色生活独立调度线程。"""
    global _scheduler
    target = engine or get_engine()
    if target is None:
        return None
    with _scheduler_lock:
        if _scheduler is not None:
            thread = _scheduler._thread
            if (_scheduler._engine is target and not _scheduler._stop.is_set()
                    and thread and thread.is_alive()):
                return _scheduler
            prev = _scheduler
        else:
            prev = None
    if prev is not None:
        prev.stop()
    sched = RoleLifeScheduler(target, tick_seconds=tick_seconds)
    with _scheduler_lock:
        _scheduler = sched
    sched.start()
    return sched


def stop_role_life_scheduler(timeout: float = 3.0) -> None:
    global _scheduler
    with _scheduler_lock:
        sched = _scheduler
    if sched is not None:
        sched.stop(timeout=timeout)
        with _scheduler_lock:
            if _scheduler is sched and not (sched._thread and sched._thread.is_alive()):
                _scheduler = None


__all__ = [
    "DEFAULT_ROLE_CONFIG",
    "GLOBAL_ACCOUNT",
    "GLOBAL_ROLE",
    "JOB_KINDS",
    "LifeBudgetDenied",
    "LifeGenerationUnknown",
    "LifeNotSent",
    "LifeRequestRejected",
    "RoleLifeCorruptError",
    "LifeJob",
    "RoleLifeEngine",
    "RoleLifeScheduler",
    "get_engine",
    "config_revision",
    "migrate_legacy_global_state",
    "normalize_role_config",
    "normalize_role_life_section",
    "resolve_global_config",
    "role_config",
    "sanitize_chat_life_note",
    "set_engine",
    "start_role_life_scheduler",
    "stop_role_life_scheduler",
]
