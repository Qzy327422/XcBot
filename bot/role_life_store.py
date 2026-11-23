# -*- coding: utf-8 -*-
"""角色生活的有界持久化。

每个 (机器人账号, 稳定角色 ID) 一份 `data/role_life/<account>/<key>/state.json`。
读改写全程持进程内 per-role 锁，落盘复用 `atomic_write_json` 的跨进程文件锁。
本模块只做校验、读写和裁剪，不调用模型、不等待网络。

错误语义：
- 文件不存在 → 空骨架（读操作不创建文件）。
- 已存在的文件必须满足完整合法 shape：schema/account/role_id、整数字段、
  budget.calls、全部集合字段。`{}`、缺身份、坏 budget 都抛
  `RoleLifeCorruptError`，原字节保持不变，绝不当成空账本。
- 调用方必须暂停该角色并等管理员在 WebUI 明确清空，否则一次坏读就会把
  预算、去重记录和旧人生一起洗掉。

并发语义：
- `update` 在锁内执行 mutator；提供 `expect` 时先在锁内做 CAS 校验。
- 若归一化前后内容完全相同则不写盘（稳态 tick 0 次 fsync）。
- 内容真正变化时才把 `state_version` 加一，供外部写操作做乐观并发控制。
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time as _time
from pathlib import Path
from typing import Any, Callable, Mapping

from bot.io_json import atomic_write_json
from bot.paths import DATA_DIR
from bot.persona import persona_role_key

SCHEMA_VERSION = 1

MAX_EXPERIENCES = 300
MAX_PLANS = 7
MAX_JOBS = 200
MAX_LIFE_NOTES = 40
MAX_DELIVERY_ITEMS = 64
MAX_DELIVERY_TARGETS = 256
MAX_DELIVERY_TICKETS = 128
MAX_BUDGET_CALLS = 10000
MAX_TEXT_LENGTH = 4000
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_DEDUPE_KEYS = 2000
MAX_OP_REQUESTS = 256
MAX_CLEAR_REQUESTS = 16

# 合法状态文件必须具备的集合字段及类型；缺任何一个都视为损坏。
_REQUIRED_COLLECTIONS: dict[str, type] = {
    "plans": dict,
    "experiences": list,
    "jobs": dict,
    "job_order": list,
    "deliveries": dict,
    "receipts": dict,
    "sleep": dict,
    "mood": dict,
    "metrics": dict,
    "clear_requests": list,
    "dedupe": list,
    "op_requests": list,
}

GLOBAL_ACCOUNT = "global"
GLOBAL_ROLE = "default"
_ACCOUNT_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

_LOCK_REGISTRY: dict[str, threading.RLock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()


class RoleLifeCorruptError(RuntimeError):
    """状态文件损坏或身份不匹配；必须人工处理，不能自动重置。"""


class RoleLifeConflict(RuntimeError):
    """锁内 CAS 失败（epoch/state_version/revision 不匹配）。"""


class _Expect:
    __slots__ = ("epoch", "state_version", "revision")

    def __init__(self, epoch: int | None = None, state_version: int | None = None,
                 revision: str = "") -> None:
        self.epoch = epoch
        self.state_version = state_version
        self.revision = str(revision or "")


def _role_lock(path: str) -> threading.RLock:
    with _LOCK_REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(path)
        if lock is None:
            lock = threading.RLock()
            _LOCK_REGISTRY[path] = lock
        return lock


def validate_account(account: str) -> str:
    """接受统一全局角色生活标识 `global` 或正十进制 QQ 号；不接受任意名字拼路径。"""
    text = str(account if account is not None else "").strip()
    if text == GLOBAL_ACCOUNT:
        return GLOBAL_ACCOUNT
    if not _ACCOUNT_RE.match(text):
        raise ValueError("account must be 'global' or a positive decimal QQ id")
    return text


def role_dir(base_dir: str | Path, account: str, role_id: str) -> Path:
    return Path(base_dir) / validate_account(account) / persona_role_key(role_id)


def empty_state(account: str, role_id: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "account": account,
        "role_id": role_id,
        "epoch": 0,
        "state_version": 0,
        "plan_serial": 0,
        "last_advance": 0,
        "recovered_from_corrupt": False,
        "corrupt_backup": "",
        "budget_unknown_until": 0,
        "revision": "",
        "config_revision": "",
        "budget": {"calls": [], "role_daily_limit": 24},
        "clear_requests": [],
        "dedupe": [],
        "op_requests": [],
        "plans": {},
        "experiences": [],
        "sleep": {},
        "mood": {},
        "life_notes": [],
        "jobs": {},
        "job_order": [],
        "deliveries": {},
        "receipts": {},
        "metrics": {},
    }


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _clip(text: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    return str(text or "")[:limit]


def _valid_item(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and bool(str(item.get("id", "") or ""))
        and bool(str(item.get("title", "") or ""))
        and _finite(item.get("start"))
        and _finite(item.get("end"))
        and item["end"] > item["start"]
        and item.get("status") in ("planned", "active", "completed", "interrupted", "skipped")
    )


def _reject(message: str) -> None:
    raise RoleLifeCorruptError(message)


def _validate_raw(raw: Any, account: str, role_id: str) -> dict[str, Any]:
    """已存在文件的严格 shape 校验；不依赖后续 normalize 兜底。"""
    if not isinstance(raw, dict):
        _reject("state is not an object")
    if raw.get("schema") != SCHEMA_VERSION:
        _reject(f"unsupported schema: {raw.get('schema')!r}")
    if raw.get("account") != account or raw.get("role_id") != role_id:
        _reject("state identity mismatch")
    for name in ("epoch", "state_version", "plan_serial"):
        value = raw.get(name)
        if type(value) is not int or value < 0:
            _reject(f"state.{name} must be a non-negative integer")
    if not _finite(raw.get("last_advance", 0)) or raw.get("last_advance", 0) < 0:
        _reject("state.last_advance must be a non-negative number")
    budget = raw.get("budget")
    if not isinstance(budget, dict):
        _reject("state.budget must be an object")
    calls = budget.get("calls")
    if not isinstance(calls, list):
        _reject("state.budget.calls must be a list")
    for stamp in calls:
        if not _finite(stamp) or stamp < 0:
            _reject("state.budget.calls contains an invalid timestamp")
    limit = budget.get("role_daily_limit")
    if type(limit) is not int or not 0 < limit <= 100000:
        _reject("state.budget.role_daily_limit is invalid")
    for name, expected_type in _REQUIRED_COLLECTIONS.items():
        if not isinstance(raw.get(name), expected_type):
            _reject(f"state.{name} must be a {expected_type.__name__}")
    if "life_notes" in raw and not isinstance(raw.get("life_notes"), list):
        _reject("state.life_notes must be a list")
    if not isinstance(raw.get("revision", ""), str) or not isinstance(
            raw.get("config_revision", ""), str):
        _reject("state revisions must be strings")
    return raw


def _normalize_plan(plan: Any) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    items = plan.get("items")
    if not isinstance(items, list):
        return None
    clean = [copy.deepcopy(i) for i in items if _valid_item(i)]
    serial = plan.get("serial")
    return {
        "plan_id": _clip(plan.get("plan_id"), 64),
        "serial": serial if type(serial) is int and serial >= 0 else 0,
        "generated_at": plan.get("generated_at") if _finite(plan.get("generated_at")) else 0,
        "items": clean[:12],
    }


def _normalize_experience(exp: Any) -> dict[str, Any] | None:
    if not isinstance(exp, dict):
        return None
    kind = exp.get("kind")
    if kind not in ("activity", "diary", "dream"):
        return None
    text = str(exp.get("text", "") or "").strip()
    if not text:
        return None
    sources = exp.get("source_ids")
    experience_id = _clip(exp.get("id"), 64)
    if not experience_id:
        return None
    return {
        "id": experience_id,
        "kind": kind,
        "date": _clip(exp.get("date"), 10),
        "created_at": exp.get("created_at") if _finite(exp.get("created_at")) else 0,
        "text": text[:1200],
        "source_ids": [str(x)[:64] for x in sources if isinstance(x, (str, int))][:16]
        if isinstance(sources, list) else [],
        "origin": "role_simulation",
        "dream": kind == "dream",
    }


def _normalize_deliveries(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key, entry in raw.items():
        if len(result) >= MAX_DELIVERY_TARGETS:
            break
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        attempts = entry.get("attempts")
        if not isinstance(attempts, dict):
            attempts = {}
        clean_attempts = {
            str(k)[:64]: v for k, v in attempts.items()
            if v in ("sent", "partial", "unknown", "failed")
        }
        clean_attempts = dict(list(clean_attempts.items())[-MAX_DELIVERY_ITEMS:])
        ids = entry.get("item_ids")
        result[key[:160]] = {
            "item_ids": [str(x)[:64] for x in ids if isinstance(x, (str, int))][-MAX_DELIVERY_ITEMS:]
            if isinstance(ids, list) else [],
            "attempts": clean_attempts,
        }
    return result


def _normalize_receipts(raw: Any) -> dict[str, Any]:
    """投递票据：ticket -> {account, target, items, status, epoch, state_version, request_id}。"""
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for ticket, entry in list(raw.items())[-MAX_DELIVERY_TICKETS:]:
        if not isinstance(ticket, str) or not isinstance(entry, dict):
            continue
        items = entry.get("items")
        result[ticket[:80]] = {
            "account": _clip(entry.get("account"), 24),
            "target": _clip(entry.get("target"), 160),
            "items": [str(x)[:64] for x in items if isinstance(x, (str, int))][:MAX_DELIVERY_ITEMS]
            if isinstance(items, list) else [],
            "status": entry.get("status") if entry.get("status") in
            ("pending", "sent", "partial", "unknown", "failed") else "pending",
            "epoch": entry.get("epoch") if type(entry.get("epoch")) is int else 0,
            "state_version": entry.get("state_version")
            if type(entry.get("state_version")) is int else 0,
            "request_id": _clip(entry.get("request_id"), 64),
            "payload_hash": _clip(entry.get("payload_hash"), 32),
        }
    return result


def _normalize_op_requests(raw: Any) -> list[dict[str, Any]]:
    """幂等记录：op + request_id + payload_hash -> job_id/ticket/status。"""
    if not isinstance(raw, list):
        return []
    clean = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        clean.append({
            "op": _clip(entry.get("op"), 32),
            "request_id": _clip(entry.get("request_id"), 64),
            "payload_hash": _clip(entry.get("payload_hash"), 32),
            "result": _clip(entry.get("result"), 80),
            "epoch": entry.get("epoch") if type(entry.get("epoch")) is int else 0,
        })
    return clean[-MAX_OP_REQUESTS:]


def normalize_job(job: Any) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    kind = job.get("kind")
    if kind not in ("plan", "activity", "diary", "dream", "maintenance"):
        return None
    status = job.get("status")
    if status not in ("pending", "running", "succeeded", "failed", "unknown"):
        return None
    args = job.get("args")
    return {
        "id": _clip(job.get("id"), 64),
        "kind": kind,
        "status": status,
        "created_at": job.get("created_at") if _finite(job.get("created_at")) else 0,
        "started_at": job.get("started_at") if _finite(job.get("started_at")) else 0,
        "finished_at": job.get("finished_at") if _finite(job.get("finished_at")) else 0,
        "args": copy.deepcopy(args) if isinstance(args, dict) else {},
        "error": _clip(job.get("error"), 300),
        "result_id": _clip(job.get("result_id"), 64),
        "revision": _clip(job.get("revision"), 64),
        "config_revision": _clip(job.get("config_revision"), 64),
        "epoch": job.get("epoch") if type(job.get("epoch")) is int else 0,
        "attempts": job.get("attempts") if type(job.get("attempts")) is int else 0,
        "generation": job.get("generation") if type(job.get("generation")) is int else 0,
        "dedupe_key": _clip(job.get("dedupe_key"), 160),
        "request_id": _clip(job.get("request_id"), 64),
        "retry_of": _clip(job.get("retry_of"), 64),
        "retried_at": job.get("retried_at") if _finite(job.get("retried_at")) else 0,
        "retried_by": _clip(job.get("retried_by"), 64),
    }


def _normalize_budget(raw: Any) -> dict[str, Any]:
    # 调用前 _validate_raw 已保证 shape；这里只做有界裁剪。
    calls = raw.get("calls")
    clean_calls = [t for t in calls if _finite(t) and t >= 0][-MAX_BUDGET_CALLS:] \
        if isinstance(calls, list) else []
    limit = raw.get("role_daily_limit")
    return {
        "calls": clean_calls,
        "role_daily_limit": limit if type(limit) is int and 0 < limit <= 100000 else 24,
    }


def _normalize_jobs(
    state: dict[str, Any],
    manual_note_texts: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    jobs = state.get("jobs")
    order = state.get("job_order")
    clean_jobs: dict[str, Any] = {}
    if isinstance(jobs, dict):
        for key, job in jobs.items():
            normalized = normalize_job(job)
            if normalized is not None:
                if manual_note_texts is not None:
                    args = normalized["args"]
                    if normalized["kind"] == "plan" and isinstance(args.get("life_notes"), list):
                        filtered = [
                            x for x in args["life_notes"]
                            if isinstance(x, str) and x in manual_note_texts
                        ]
                        if filtered:
                            args["life_notes"] = filtered
                        else:
                            args.pop("life_notes", None)
                    elif normalized["kind"] == "diary" and isinstance(args.get("material"), list):
                        args["material"] = [
                            e for e in args["material"]
                            if not (
                                isinstance(e, dict)
                                and e.get("kind") == "note"
                                and str(e.get("text") or "") not in manual_note_texts
                            )
                        ]
                clean_jobs[normalized["id"] or str(key)[:64]] = normalized
    ordered: list[str] = []
    if isinstance(order, list):
        for job_id in order:
            job_id = str(job_id)[:64]
            if job_id in clean_jobs and job_id not in ordered:
                ordered.append(job_id)
    for job_id in sorted(clean_jobs, key=lambda k: clean_jobs[k]["created_at"]):
        if job_id not in ordered:
            ordered.append(job_id)
    ordered = ordered[-MAX_JOBS:]
    return {job_id: clean_jobs[job_id] for job_id in ordered}, ordered


def _normalize_life_note(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text", "") or "").strip()[:120]
    if not text:
        return None
    note_id = _clip(raw.get("id"), 64)
    if not note_id:
        return None
    source = raw.get("source")
    return {
        "id": note_id,
        "date": _clip(raw.get("date"), 10),
        "text": text,
        "created_at": raw.get("created_at") if _finite(raw.get("created_at")) else 0,
        "source": source if source in ("chat_feedback", "manual") else "chat_feedback",
        "scope": _clip(raw.get("scope"), 64),
    }


def normalize_state(state: Any, account: str, role_id: str) -> dict[str, Any]:
    """把已确认身份的内存内容裁剪成有界合法状态；未知字段丢弃。"""
    if not isinstance(state, dict):
        raise RoleLifeCorruptError("state is not an object")
    base = empty_state(account, role_id)
    base["epoch"] = max(0, state.get("epoch", 0)) if type(state.get("epoch")) is int else 0
    base["state_version"] = (max(0, state.get("state_version", 0))
                             if type(state.get("state_version")) is int else 0)
    base["plan_serial"] = (max(0, state.get("plan_serial", 0))
                           if type(state.get("plan_serial")) is int else 0)
    base["last_advance"] = (state.get("last_advance")
                            if _finite(state.get("last_advance")) else 0)
    base["recovered_from_corrupt"] = bool(state.get("recovered_from_corrupt"))
    base["corrupt_backup"] = _clip(state.get("corrupt_backup"), 120)
    unknown_until = state.get("budget_unknown_until")
    base["budget_unknown_until"] = (unknown_until
                                    if _finite(unknown_until) and unknown_until >= 0 else 0)
    base["revision"] = _clip(state.get("revision"), 64)
    base["config_revision"] = _clip(state.get("config_revision"), 64)
    base["budget"] = _normalize_budget(state.get("budget") or {})
    clears = state.get("clear_requests")
    if isinstance(clears, list):
        base["clear_requests"] = [str(x)[:64] for x in clears if str(x)][-MAX_CLEAR_REQUESTS:]
    dedupe = state.get("dedupe")
    if isinstance(dedupe, list):
        keys = [str(x)[:160] for x in dedupe if str(x)]
        base["dedupe"] = list(dict.fromkeys(keys))[-MAX_DEDUPE_KEYS:]
    base["op_requests"] = _normalize_op_requests(state.get("op_requests"))

    plans = state.get("plans")
    if isinstance(plans, dict):
        clean_plans = {}
        for date, plan in sorted(plans.items(), key=lambda kv: str(kv[0]))[-MAX_PLANS:]:
            if isinstance(date, str) and _DATE_RE.match(date):
                normalized = _normalize_plan(plan)
                if normalized is not None:
                    clean_plans[date] = normalized
        base["plans"] = clean_plans

    experiences = state.get("experiences")
    if isinstance(experiences, list):
        clean = [e for e in (_normalize_experience(x) for x in experiences) if e]
        clean.sort(key=lambda e: e["created_at"])
        base["experiences"] = clean[-MAX_EXPERIENCES:]

    sleep = state.get("sleep")
    if (isinstance(sleep, dict) and _finite(sleep.get("start")) and _finite(sleep.get("end"))
            and sleep["end"] > sleep["start"]):
        base["sleep"] = {
            "date": _clip(sleep.get("date"), 10),
            "start": sleep["start"],
            "end": sleep["end"],
            "wake_dream_done": bool(sleep.get("wake_dream_done")),
        }

    mood = state.get("mood")
    if isinstance(mood, dict) and str(mood.get("mood", "") or ""):
        sources = mood.get("source_ids")
        base["mood"] = {
            "mood": _clip(mood.get("mood"), 40),
            "energy": _clip(mood.get("energy"), 40),
            "source_ids": [str(x)[:64] for x in sources][:16] if isinstance(sources, list) else [],
            "updated_at": mood.get("updated_at") if _finite(mood.get("updated_at")) else 0,
        }

    notes = state.get("life_notes")
    if isinstance(notes, list):
        clean_notes = [
            n for n in (_normalize_life_note(x) for x in notes)
            if n and n.get("source") == "manual" and not n.get("scope")
        ]
        clean_notes.sort(key=lambda n: n["created_at"])
        base["life_notes"] = clean_notes[-MAX_LIFE_NOTES:]

    manual_note_texts = {
        n["text"] for n in base["life_notes"]
        if n.get("source") == "manual" and not n.get("scope")
    }
    base["jobs"], base["job_order"] = _normalize_jobs(state, manual_note_texts=manual_note_texts)
    base["deliveries"] = _normalize_deliveries(state.get("deliveries"))
    base["receipts"] = _normalize_receipts(state.get("receipts"))

    metrics = state.get("metrics")
    if isinstance(metrics, dict):
        base["metrics"] = {
            "last_error": _clip(metrics.get("last_error"), 300),
            "last_error_at": metrics.get("last_error_at") if _finite(metrics.get("last_error_at")) else 0,
            "last_success_at": metrics.get("last_success_at") if _finite(metrics.get("last_success_at")) else 0,
        }
    return base


def _salvage_budget(raw_bytes: bytes) -> dict[str, Any] | None:
    """从损坏但仍是 JSON 的文件里独立抢救 budget；任何不确定都返回 None。"""
    if not raw_bytes:
        return None
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError, OverflowError):
        return None
    if not isinstance(raw, dict):
        return None
    budget = raw.get("budget")
    if not isinstance(budget, dict):
        return None
    calls = budget.get("calls")
    limit = budget.get("role_daily_limit")
    if not isinstance(calls, list):
        return None
    if not all(_finite(t) and t >= 0 for t in calls):
        return None
    if type(limit) is not int or not 0 < limit <= 100000:
        return None
    return {"calls": list(calls)[-MAX_BUDGET_CALLS:], "role_daily_limit": limit}


class RoleLifeStore:
    """按 (account, role_id) 隔离的状态仓库。"""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else Path(DATA_DIR) / "role_life"

    def state_path(self, account: str, role_id: str) -> Path:
        return role_dir(self.base_dir, account, role_id) / "state.json"

    def _read(self, account: str, role_id: str) -> dict[str, Any]:
        path = self.state_path(account, role_id)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return empty_state(account, role_id)
        except OSError as exc:
            raise RoleLifeCorruptError(f"state unreadable: {exc}") from exc
        if size > MAX_STATE_BYTES:
            raise RoleLifeCorruptError("state file too large")
        try:
            with path.open("r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            raise RoleLifeCorruptError(f"state unreadable: {exc}") from exc
        return _validate_raw(raw, account, role_id)

    def load(self, account: str, role_id: str) -> dict[str, Any]:
        """只读加载；不创建文件。严格 shape 不合法抛 RoleLifeCorruptError。"""
        account = validate_account(account)
        path = self.state_path(account, role_id)
        lock = _role_lock(str(path))
        with lock:
            return normalize_state(self._read(account, role_id), account, role_id)

    def _backup_and_recover(self, account: str, role_id: str) -> dict[str, Any]:
        """损坏文件恢复：先备份原始字节，再尽量抢救可独立校验的预算账本。

        - 原始字节完整保留在同目录 `state.corrupt-<ms>.json.bak`，绝不销毁。
        - 能从损坏 JSON 里独立解析出合法 budget 就保留；完全不可读则
          `budget_unknown_until = now + 24h`，封住本角色额度，账号账本不动。
        """
        path = self.state_path(account, role_id)
        state = empty_state(account, role_id)
        state["recovered_from_corrupt"] = True
        raw_bytes = b""
        try:
            raw_bytes = path.read_bytes()
        except OSError:
            raw_bytes = b""
        if raw_bytes:
            backup = path.with_name(f"state.corrupt-{int(_time.time() * 1000)}.json.bak")
            for index in range(20):
                target = backup if index == 0 else backup.with_name(
                    f"{backup.stem}-{index}.bak")
                try:
                    with target.open("xb") as stream:
                        stream.write(raw_bytes)
                    state["corrupt_backup"] = target.name
                    break
                except FileExistsError:
                    continue
                except OSError:
                    break
        salvaged = _salvage_budget(raw_bytes)
        if salvaged is not None:
            state["budget"] = salvaged
        else:
            state["budget_unknown_until"] = _time.time() + 86400
        return state

    def update(self, account: str, role_id: str,
               mutator: Callable[[dict[str, Any]], Any], *, force: bool = False,
               expect: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """持锁读改写；mutator 只在内存里操作，禁止网络与 await。

        - `expect={"epoch":n,"state_version":n,"revision":s}`：锁内 CAS，
          不匹配抛 `RoleLifeConflict`，mutator 不会执行。
        - 归一化前后完全相等时不写盘，也不推进 state_version。
        - 内容变化时 state_version + 1；写盘失败抛异常，调用方视为未提交。
        """
        account = validate_account(account)
        path = self.state_path(account, role_id)
        lock = _role_lock(str(path))
        with lock:
            try:
                state = normalize_state(self._read(account, role_id), account, role_id)
            except RoleLifeCorruptError:
                if not force:
                    raise
                state = self._backup_and_recover(account, role_id)
            if expect is not None:
                _check_expect(state, expect)
            before = copy.deepcopy(state)
            mutator(state)
            clean = normalize_state(state, account, role_id)
            if clean == before:
                return copy.deepcopy(clean)
            clean["state_version"] = int(before.get("state_version") or 0) + 1
            atomic_write_json(str(path), clean, indent=2)
            return copy.deepcopy(clean)

    def save(self, account: str, role_id: str, state: dict[str, Any]) -> dict[str, Any]:
        account = validate_account(account)
        path = self.state_path(account, role_id)
        lock = _role_lock(str(path))
        with lock:
            clean = normalize_state(state, account, role_id)
            atomic_write_json(str(path), clean, indent=2)
            return copy.deepcopy(clean)

    def snapshot(self, account: str, role_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.load(account, role_id))

    def try_load(self, account: str, role_id: str) -> tuple[dict[str, Any] | None, str]:
        """WebUI/聊天只读入口：返回 (state, error)。损坏时不抛给界面。"""
        try:
            return self.load(account, role_id), ""
        except RoleLifeCorruptError as exc:
            return None, str(exc)
        except ValueError as exc:
            return None, str(exc)

    def clear(self, account: str, role_id: str, *, reason: str = "",
              request_id: str = "", expect: Mapping[str, Any] | None = None,
              allow_corrupt: bool = False,
              op_request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """清空生活内容：保留预算账本、投递防重账与票据，epoch+1 使在途结果作废。

        - 正常状态：`expect` 必填并在锁内 CAS。
        - 损坏状态：只有显式 `allow_corrupt=True` 才覆盖重建；恢复前先备份
          原文件，能独立校验 budget 就保留，否则封住本角色 24h 额度。
        - 清空动作与 `op_request` 幂等记录在**同一次 store.update 事务**内完成；
          重复 request_id 不产生第二次写盘。
        """
        state, _err = self.try_load(account, role_id)
        if state is None and not allow_corrupt:
            raise RoleLifeCorruptError("state corrupt; explicit allow_corrupt required")

        def mutate(state: dict[str, Any]) -> None:
            ops = state.setdefault("op_requests", [])
            if op_request is not None:
                op = str(op_request.get("op") or "")
                rid = str(op_request.get("request_id") or "")
                if any(isinstance(e, dict) and e.get("op") == op
                       and e.get("request_id") == rid for e in ops):
                    return  # 幂等：完全相同请求已处理，内容不变 -> 不写盘
            state["epoch"] = int(state.get("epoch", 0)) + 1
            state["plans"] = {}
            state["experiences"] = []
            state["sleep"] = {}
            state["mood"] = {}
            state["life_notes"] = []
            state["jobs"] = {}
            state["job_order"] = []
            state["metrics"] = {"last_error": _clip(reason, 300)} if reason else {}
            if request_id:
                requests = state.setdefault("clear_requests", [])
                if request_id not in requests:
                    requests.append(request_id[:64])
                state["clear_requests"] = requests[-MAX_CLEAR_REQUESTS:]
            if op_request is not None:
                ops.append(copy.deepcopy(dict(op_request)))
                state["op_requests"] = ops[-MAX_OP_REQUESTS:]

        return self.update(account, role_id, mutate, force=allow_corrupt, expect=expect)


def _check_expect(state: Mapping[str, Any], expect: Mapping[str, Any]) -> None:
    epoch = expect.get("epoch")
    version = expect.get("state_version")
    revision = str(expect.get("revision") or "")
    if epoch is not None and int(state.get("epoch") or 0) != int(epoch):
        raise RoleLifeConflict("epoch mismatch")
    if version is not None and int(state.get("state_version") or 0) != int(version):
        raise RoleLifeConflict("state_version mismatch")
    stored_revision = str(state.get("revision") or "")
    if revision and stored_revision and stored_revision != revision:
        raise RoleLifeConflict("revision mismatch")
    # stored 为空表示尚未首次推进；调用方已用 effective persona revision 校验过。


def prune_runtime_state(state: dict[str, Any], now: float, retention_days: int) -> None:
    """按时间裁剪（原地）：旧计划、过保留期的经历与预算时间戳。"""
    cutoff = now - max(1, retention_days) * 86400
    plans = state.get("plans")
    if isinstance(plans, dict):
        keep = {}
        for date, plan in plans.items():
            generated = plan.get("generated_at") if isinstance(plan, dict) else 0
            if _finite(generated) and generated < cutoff - 7 * 86400:
                continue
            keep[date] = plan
        state["plans"] = dict(sorted(keep.items())[-MAX_PLANS:])
    experiences = state.get("experiences")
    if isinstance(experiences, list):
        state["experiences"] = [
            e for e in experiences
            if not _finite(e.get("created_at")) or e.get("created_at") >= cutoff
        ][-MAX_EXPERIENCES:]
    notes = state.get("life_notes")
    if isinstance(notes, list):
        state["life_notes"] = [
            n for n in notes
            if not _finite(n.get("created_at")) or n.get("created_at") >= cutoff
        ][-MAX_LIFE_NOTES:]
    budget = state.get("budget")
    if isinstance(budget, dict) and isinstance(budget.get("calls"), list):
        budget["calls"] = [t for t in budget["calls"] if now - t < 86400][-MAX_BUDGET_CALLS:]


def migrate_legacy_global_state(store: RoleLifeStore) -> bool:
    """若尚未生成统一全局生活档案，则把最新一份旧版按 QQ 号/人设存下的状态迁移过来。

    原旧文件保留在磁盘上不删，方便人工排查；已存在全局档案时直接跳过。
    """
    target_path = store.state_path(GLOBAL_ACCOUNT, GLOBAL_ROLE)
    lock = _role_lock(str(target_path))
    with lock:
        if target_path.exists():
            return False
        base = store.base_dir
        if not base.exists() or not base.is_dir():
            return False
        candidates: list[tuple[float, Path]] = []
        try:
            for acct_dir in base.iterdir():
                if not acct_dir.is_dir() or not _ACCOUNT_RE.match(acct_dir.name):
                    continue
                for role_sub in acct_dir.iterdir():
                    if not role_sub.is_dir():
                        continue
                    candidate = role_sub / "state.json"
                    try:
                        st = candidate.stat()
                    except OSError:
                        continue
                    if st.st_size <= 0 or st.st_size > MAX_STATE_BYTES:
                        continue
                    candidates.append((st.st_mtime, candidate))
        except OSError:
            return False
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        for _mtime, path in candidates:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
                    continue
                migrated = normalize_state(raw, GLOBAL_ACCOUNT, GLOBAL_ROLE)
                atomic_write_json(str(target_path), migrated, indent=2)
                return True
            except Exception:
                continue
        return False
