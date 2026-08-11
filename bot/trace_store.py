# -*- coding: utf-8 -*-
"""AI 对话追踪记录（JSON 摘要索引 + SQLite 详情，固定条数环形缓冲）。

只记录 AI 对话的调用链路，用于在 WebUI 追踪页排查：
消息发送链路、模型调用链路（含失败重试）、当次系统提示词、
Agent 工具调用历史（工具名/参数/结果）、单条 token 统计。

体积与内存控制：
- 记录条数上限默认 100 条，WebUI 可调，超出即淘汰最旧记录。
- 内存只常驻列表摘要；完整详情暂存在待写队列，后台批量写入单个 SQLite 文件。
- JSON 只保存 WebUI 列表所需的摘要，详情页按 trace id 从 SQLite 读取全文。
- 每类文本仍有截断上限，单条记录的磁盘体积有明确上界。

隐私说明：这里**会**持久化对话原文——系统提示词、用户消息、模型回复、上下文
历史和工具调用参数/结果都能在追踪页完整查看。默认关闭；开启后 data/ 目录下的
追踪 JSON 与 SQLite 文件包含聊天内容，分享日志或备份前请注意。
"""
from __future__ import annotations

import atexit
import copy
import json
import sqlite3
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

from bot.io_json import atomic_write_json
from bot.paths import BASE_DIR


def classify_error(text: str) -> str:
    """把错误文本粗分类，供前端显示模型切换原因。

    分类口径与 main.py 里 agen_content 的重试分支保持一致，
    这样不必改动那些分支就能在追踪页看出为什么切了模型。
    """
    msg = str(text or "").lower()
    if not msg:
        return "other"
    if "llm 回复命中切换关键词" in msg:
        return "failover_keyword"
    if "429" in msg or "rate limit" in msg or "rpm limit" in msg:
        return "rate_limit"
    if "503" in msg or "busy" in msg:
        return "busy"
    if "quota" in msg or "insufficient" in msg or "balance" in msg or "402" in msg:
        return "quota"
    if "invalid" in msg or "unauthorized" in msg or "401" in msg:
        return "auth"
    if "model not exist" in msg or "not support" in msg or "404" in msg:
        return "model_missing"
    if "500" in msg or "502" in msg or "504" in msg or "timeout" in msg or "403" in msg:
        return "server"
    if "choices" in msg:
        return "choices"
    return "other"


class TraceStore:
    """AI 对话追踪存储。线程安全，节流落盘。

    只按记录条数控制体积：deque(maxlen) 自动挤掉最旧的，
    无窗口扫描、无去重池、无引用回收，写入是 O(1)。
    """

    SAVE_PATH = Path(str(BASE_DIR)) / "data" / "ai_trace.json"
    # 节流：落盘要序列化全部记录，低配机上不宜太密
    SAVE_INTERVAL_SECONDS = 30.0
    # 记录条数上限。唯一的体积控制维度，WebUI 可调（1~1000）。
    DEFAULT_MAX_RECORDS = 100
    MIN_MAX_RECORDS = 1
    LIMIT_MAX_RECORDS = 1000
    # 单条记录最多保留的重试链路条数
    MAX_ATTEMPTS = 20
    # 新增详情达到此数量时提前唤醒后台线程，限制 30 秒窗口内的内存峰值。
    PENDING_FLUSH_THRESHOLD = 20
    # v4 起：JSON 仅存列表摘要，完整详情放单个 SQLite 文件。
    # 相比每条一个 JSON 文件，SQLite 可在一次事务中批量写入，避免大量 fsync/小文件。
    DETAIL_DB_SUFFIX = "_details.sqlite3"

    SYSTEM_PROMPT_MAX = 6000
    TEXT_MAX = 4000
    ERROR_MAX = 400
    PREVIEW_MAX = 80
    # 历史条目：条数与单条长度都比当前消息收得更紧。
    # 详情改存 SQLite 后磁盘仍需控制单条体积；历史会让每条记录携带一份完整
    # 上下文），而排查时看历史只需要知道大概说了什么，不需要全文。
    HISTORY_ITEMS_MAX = 30
    HISTORY_TEXT_MAX = 1500
    # Agent 工具调用明细：条数上限 + 单条参数/结果的截断长度。
    # 30 轮循环每轮可能并发多个工具，不限条数会让单条记录膨胀。
    TOOL_CALLS_MAX = 40
    TOOL_ARGS_MAX = 1000
    TOOL_RESULT_MAX = 2000
    # Follow-Up 注入明细（用户在 Agent 执行途中插的话）
    FOLLOW_UPS_MAX = 20
    FOLLOW_UP_TEXT_MAX = 500

    def __init__(self, path: Optional[Path] = None, max_records: Optional[int] = None):
        self.path = Path(path) if path else self.SAVE_PATH
        # 默认关闭：避免升级后静默开始落盘用户对话内容
        self._enabled = False
        self._max_records_explicit = max_records is not None
        self._max_records = self._clamp_max_records(max_records)
        # v4 起 records 只保存 WebUI 列表所需的摘要，完整正文进 SQLite。
        self.records: deque = deque(maxlen=self._max_records)
        self.detail_db_path = self.path.with_name(self.path.stem + self.DETAIL_DB_SUFFIX)
        # 30 秒后台 flush 前的新增/回填详情；落盘后立即清空，常驻内存不随总记录数增长。
        self._pending_details: dict[str, dict] = {}
        # 索引已淘汰、待从 SQLite 删除的记录 id。删除在索引成功写入之后执行，
        # 崩溃时最多留下孤儿行，不会让索引指向已删除详情。
        self._pending_deletes: set[str] = set()
        self.last_update = time.time()
        self._dirty = False
        # 每次内存状态变更递增；用于识别磁盘提交期间发生的新修改。
        self._state_version = 0
        self._last_save = 0.0
        self._lock = threading.RLock()
        # 磁盘提交独立串行；提交期间不占状态锁，避免慢磁盘堵住 Agent 新 trace 写入。
        self._save_lock = threading.Lock()
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_flush = threading.Event()
        self._flush_wakeup = threading.Event()
        self.load()

    @classmethod
    def _clamp_max_records(cls, value: Any) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return cls.DEFAULT_MAX_RECORDS
        return max(cls.MIN_MAX_RECORDS, min(n, cls.LIMIT_MAX_RECORDS))

    # ==================== 开关 ====================

    @property
    def enabled(self) -> bool:
        """主流程热路径只读这个属性，零文件 IO。"""
        return bool(self._enabled)

    @property
    def max_records(self) -> int:
        return self._max_records

    def set_enabled(self, value: bool) -> bool:
        with self._lock:
            self._enabled = bool(value)
            self.last_update = time.time()
            self._state_version += 1
            self._dirty = True
            enabled = self._enabled
        self.save(force=True)
        return enabled

    def set_max_records(self, value: Any) -> int:
        """调整条数上限。deque 换新的，超出部分自动丢最旧的。"""
        new_max = self._clamp_max_records(value)
        with self._lock:
            if new_max == self._max_records:
                return self._max_records
            old_ids = {str(r.get("id") or "") for r in self.records}
            self._max_records = new_max
            self.records = deque(self.records, maxlen=new_max)
            kept_ids = {str(r.get("id") or "") for r in self.records}
            for rid in old_ids - kept_ids:
                if rid:
                    self._pending_details.pop(rid, None)
                    self._pending_deletes.add(rid)
            self.last_update = time.time()
            self._state_version += 1
            self._dirty = True
            result = self._max_records
        self.save(force=True)
        return result

    # ==================== 写入 ====================

    def add_record(self, record: dict) -> str:
        """写入一条追踪记录，返回 trace_id。

        只做内存操作并标记脏位，落盘交给后台 flush 线程，
        避免在 asyncio 事件循环线程上做同步 fsync 拖慢所有会话的回复。
        """
        if not isinstance(record, dict):
            return ""
        with self._lock:
            entry = self._sanitize_record(record)
            rid = str(entry.get("id") or "")
            # trace id 理论上唯一；若调用方重用，先移除旧摘要，避免一个 id 对应两行。
            if rid:
                self.records = deque(
                    (item for item in self.records if str(item.get("id") or "") != rid),
                    maxlen=self._max_records,
                )
            evicted_id = ""
            if len(self.records) >= self._max_records and self.records:
                evicted_id = str(self.records[0].get("id") or "")
            self.records.append(self._summary(entry))
            self._pending_details[rid] = entry
            # 同一个 id 可能被调用方重用；新详情优先，不能再被旧的淘汰任务删除。
            self._pending_deletes.discard(rid)
            if evicted_id and evicted_id != rid:
                self._pending_details.pop(evicted_id, None)
                self._pending_deletes.add(evicted_id)
            self.last_update = time.time()
            self._state_version += 1
            self._dirty = True
            if len(self._pending_details) >= self.PENDING_FLUSH_THRESHOLD:
                self._flush_wakeup.set()
            return rid

    def attach_send(self, trace_id: str, parts: int, message_ids: Optional[list] = None) -> bool:
        """回填分段发送结果。记录已被窗口裁掉时返回 False。"""
        key = str(trace_id or "")
        if not key:
            return False
        with self._lock:
            if not any(e.get("id") == key for e in reversed(self.records)):
                return False
            pending = self._pending_details.get(key)
            entry = copy.deepcopy(pending) if pending is not None else None

        # SQLite 读取不占状态锁，慢磁盘不会阻塞 add_record/list_records。
        if entry is None:
            entry = self._read_detail_db(key)

        ids = [mid for mid in (message_ids or [])[:20] if mid is not None]
        parts_value = max(0, int(parts or 0))
        with self._lock:
            summary = next((e for e in reversed(self.records) if e.get("id") == key), None)
            if summary is None:
                return False
            # 读库期间若出现了更新，以内存中的最新版本为准。
            current = self._pending_details.get(key)
            if current is not None:
                entry = copy.deepcopy(current)
            if entry is not None:
                entry["send"] = {
                    "parts": parts_value,
                    "message_ids": ids,
                    "time": time.time(),
                }
                # 用新对象替换，后台 save 可用对象身份判断旧快照是否仍是最新版。
                self._pending_details[key] = entry
                self._pending_deletes.discard(key)
                if len(self._pending_details) >= self.PENDING_FLUSH_THRESHOLD:
                    self._flush_wakeup.set()
            # 详情损坏/缺失时仍更新列表摘要，但不拿摘要覆盖 SQLite 中的残存数据。
            summary["send_parts"] = parts_value
            self._state_version += 1
            self._dirty = True
            return True

    # ==================== 读取（供 WebUI） ====================

    def list_records(self, limit: Optional[int] = None) -> dict:
        """返回摘要列表，全文只在 get_record 提供。"""
        with self._lock:
            try:
                count = int(limit) if limit else self._max_records
            except (TypeError, ValueError):
                count = self._max_records
            count = max(1, min(count, self._max_records))
            # records 已是摘要；深拷贝避免调用方修改内部对象。
            rows = [copy.deepcopy(e) for e in list(self.records)[-count:]]
            rows.reverse()  # 最新在前
            span_hours = 0.0
            if self.records:
                oldest = self._record_time(self.records[0])
                if oldest > 0:
                    span_hours = round(max(0.0, time.time() - oldest) / 3600.0, 1)
            return {
                "enabled": self.enabled,
                "span_hours": span_hours,
                "count": len(self.records),
                "max_records": self._max_records,
                "records": rows,
            }

    def get_record(self, trace_id: str) -> Optional[dict]:
        """返回完整记录。优先读尚未 flush 的内存详情，否则按 id 查询 SQLite。"""
        key = str(trace_id or "")
        if not key:
            return None
        with self._lock:
            summary = next((e for e in reversed(self.records) if e.get("id") == key), None)
            if summary is None:
                return None
            pending = self._pending_details.get(key)
            if pending is not None:
                return copy.deepcopy(pending)
            summary_copy = copy.deepcopy(summary)

        # 详情页读库不占状态锁；读完再检查一次，避免返回并发回填前的旧版本。
        full = self._read_detail_db(key)
        with self._lock:
            if not any(e.get("id") == key for e in reversed(self.records)):
                return None
            pending = self._pending_details.get(key)
            if pending is not None:
                return copy.deepcopy(pending)
        return copy.deepcopy(full if full is not None else summary_copy)

    def stats(self) -> dict:
        with self._lock:
            ok = sum(1 for r in self.records if r.get("ok"))
            return {
                "enabled": self.enabled,
                "count": len(self.records),
                "ok": ok,
                "failed": len(self.records) - ok,
                "max_records": self._max_records,
            }

    def clear(self) -> None:
        with self._lock:
            ids = {str(r.get("id") or "") for r in self.records}
            ids.update(self._pending_details)
            self.records.clear()
            self._pending_details.clear()
            self._pending_deletes.update(rid for rid in ids if rid)
            self.last_update = time.time()
            self._state_version += 1
            self._dirty = True
        self.save(force=True)

    # ==================== 内部 ====================

    @staticmethod
    def _clip(text: Any, limit: int) -> tuple[str, bool]:
        raw = str(text or "")
        if len(raw) <= limit:
            return raw, False
        return raw[:limit], True

    def _sanitize_attempts(self, attempts: Any) -> list[dict]:
        rows: list[dict] = []
        if not isinstance(attempts, list):
            return rows
        for item in attempts[-self.MAX_ATTEMPTS:]:
            if not isinstance(item, dict):
                continue
            error_text, _ = self._clip(item.get("error", ""), self.ERROR_MAX)
            rows.append({
                "index": int(item.get("index") or 0),
                "started_at": float(item.get("started_at") or 0.0),
                "duration_ms": int(item.get("duration_ms") or 0),
                "base_url": str(item.get("base_url") or ""),
                "model": str(item.get("model") or ""),
                "display_model": str(item.get("display_model") or ""),
                # key 只留前 6 位，完整值绝不落盘
                "key_hint": str(item.get("key_hint") or ""),
                "multimodal": bool(item.get("multimodal")),
                "message_count": int(item.get("message_count") or 0),
                "ok": bool(item.get("ok")),
                "category": str(item.get("category") or ("success" if item.get("ok") else "other")),
                "error_type": str(item.get("error_type") or ""),
                "error": error_text,
            })
        return rows

    def _sanitize_tool_calls(self, calls: Any) -> list[dict]:
        """清洗 Agent 工具调用明细。参数与结果直接内联，各自截断。"""
        rows: list[dict] = []
        if not isinstance(calls, list):
            return rows
        for item in calls[-self.TOOL_CALLS_MAX:]:
            if not isinstance(item, dict):
                continue
            raw_sent = str(item.get("sent_to_model", "") or "")
            raw_args = str(item.get("arguments", "") or "")
            raw_result = str(item.get("result", "") or "")
            sent_text, sent_cut = self._clip(raw_sent, self.TOOL_RESULT_MAX)
            args_text, args_cut = self._clip(raw_args, self.TOOL_ARGS_MAX)
            result_text, result_cut = self._clip(raw_result, self.TOOL_RESULT_MAX)
            rows.append({
                "round": int(item.get("round") or 0),
                "index": int(item.get("index") or 0),
                "name": str(item.get("name") or ""),
                "arguments": args_text,
                "args_chars": len(raw_args),
                "args_truncated": args_cut,
                "result": result_text,
                "result_chars": len(raw_result),
                "result_truncated": result_cut,
                "ok": bool(item.get("ok")),
                "status": str(item.get("status") or ("ok" if item.get("ok") else "error")),
                # side_effect_capable = 这类工具会产生副作用；
                # effect_state = 这一次到底跑到哪了（none/fired/unknown/abandoned）。
                # 只看前者会把「参数写错、handler 根本没跑」也显示成有副作用。
                "side_effect_capable": bool(item.get("side_effect_capable",
                                                     item.get("side_effect"))),
                "effect_state": str(item.get("effect_state") or "none"),
                "result_mode": str(item.get("result_mode") or "raw"),
                "overflow_path": str(item.get("overflow_path") or ""),
                "overflow_error": str(item.get("overflow_error") or ""),
                "duration_ms": int(item.get("duration_ms") or 0),
                "side_effect": bool(item.get("side_effect")),
                "streak": int(item.get("streak") or 1),
                "notice": bool(item.get("notice")),
                "provider_attempt": int(item.get("provider_attempt") or 1),
                # 超长结果被落盘时，模型实际收到的是预览 + 文件路径，
                # 与上面的原始 result 不同，两者都留着才好对照排查
                "raw_result_chars": int(item.get("raw_result_chars") or 0),
                "materialized": bool(item.get("materialized")),
                "sent_to_model": sent_text,
                "sent_chars": len(raw_sent),
                "sent_truncated": sent_cut,
            })
        return rows

    def _sanitize_follow_ups(self, items: Any) -> list[dict]:
        """清洗 Follow-Up 注入明细：用户在 Agent 执行途中插的话。

        delivered=False 表示这条插话没能送进模型上下文（循环已经结束），
        页面要区分显示，否则会让人以为 AI 收到了却没理。
        """
        rows: list[dict] = []
        if not isinstance(items, list):
            return rows
        for item in items[-self.FOLLOW_UPS_MAX:]:
            if not isinstance(item, dict):
                continue
            texts = item.get("texts")
            texts = texts if isinstance(texts, list) else []
            clipped = [self._clip(t, self.FOLLOW_UP_TEXT_MAX)[0] for t in texts if str(t or "").strip()]
            if not clipped:
                continue
            rows.append({
                "round": int(item.get("round") or 0),
                "provider_attempt": int(item.get("provider_attempt") or 1),
                "time": float(item.get("time") or 0.0),
                "texts": clipped,
                "injected_after_tool": str(item.get("injected_after_tool") or ""),
                "delivered": bool(item.get("delivered", True)),
            })
        return rows

    def _sanitize_record(self, record: dict) -> dict:
        now = time.time()
        raw_prompt = str(record.get("system_prompt", "") or "")
        raw_user = str(record.get("user_message", "") or "")
        raw_reply = str(record.get("reply", "") or "")
        prompt_text, _ = self._clip(raw_prompt, self.SYSTEM_PROMPT_MAX)
        user_text, user_truncated = self._clip(raw_user, self.TEXT_MAX)
        reply_text, reply_truncated = self._clip(raw_reply, self.TEXT_MAX)
        error_text, _ = self._clip(record.get("error", ""), self.ERROR_MAX)

        overview: list[dict] = []
        raw_overview = record.get("history_overview")
        if isinstance(raw_overview, list):
            for item in raw_overview[-self.HISTORY_ITEMS_MAX:]:
                if not isinstance(item, dict):
                    continue
                raw_content = str(item.get("content", "") or "")
                content, truncated = self._clip(raw_content, self.HISTORY_TEXT_MAX)
                overview.append({
                    "role": str(item.get("role") or "user"),
                    "chars": int(item.get("chars") or len(raw_content)),
                    "content": content,
                    "truncated": truncated,
                })

        raw_tokens = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
        tokens = {
            "total": int(raw_tokens.get("total") or 0),
            "prompt": int(raw_tokens.get("prompt") or 0),
            "completion": int(raw_tokens.get("completion") or 0),
            "cached": int(raw_tokens.get("cached") or 0),
            "relay_total": int(raw_tokens.get("relay_total") or 0),
            "relay_prompt": int(raw_tokens.get("relay_prompt") or 0),
            "relay_completion": int(raw_tokens.get("relay_completion") or 0),
        }

        attempts = self._sanitize_attempts(record.get("attempts"))
        raw_tool_calls = record.get("tool_calls") if isinstance(record.get("tool_calls"), list) else []
        tool_calls = self._sanitize_tool_calls(raw_tool_calls)
        follow_ups = self._sanitize_follow_ups(record.get("follow_ups"))
        chat_id = record.get("chat_id")

        return {
            "id": str(record.get("id") or uuid.uuid4().hex[:12]),
            "time": float(record.get("time") or now),
            "ok": bool(record.get("ok")),
            "duration_ms": int(record.get("duration_ms") or 0),
            "session_id": str(record.get("session_id") or ""),
            "context_type": str(record.get("context_type") or ""),
            "chat_id": str(chat_id) if chat_id is not None else "",
            "system_prompt": prompt_text,
            "prompt_chars": len(raw_prompt),
            "user_message": user_text,
            "user_chars": len(raw_user),
            "user_truncated": user_truncated,
            "reply": reply_text,
            "reply_chars": len(raw_reply),
            "reply_truncated": reply_truncated,
            "images": int(record.get("images") or 0),
            "history_count": int(record.get("history_count") or len(overview)),
            "history_overview": overview,
            "tokens": tokens,
            "model": str(record.get("model") or ""),
            "display_model": str(record.get("display_model") or ""),
            "attempts": attempts,
            "attempt_count": len(attempts),
            "tool_calls": tool_calls,
            # total 是真实发生的次数，kept 是受上限影响实际留下的。
            # 两个都存：列表页用 total、详情页用 kept，不写清楚会让人以为数据坏了。
            "tool_call_count": int(record.get("tool_call_count") or len(tool_calls)),
            "tool_calls_kept": len(tool_calls),
            "tool_calls_dropped": max(0, len(raw_tool_calls) - len(tool_calls)),
            "tool_calls_truncated": len(raw_tool_calls) > len(tool_calls),
            "agent_llm_calls": int(record.get("agent_llm_calls") or 0),
            "agent_rounds": max((c.get("round") or 0) for c in tool_calls) if tool_calls else 0,
            # 未截断前的真实轮数，避免详情页轮数比列表页少
            "agent_rounds_total": max(
                (int(c.get("round") or 0) for c in raw_tool_calls if isinstance(c, dict)),
                default=0,
            ),
            "retry_count": max(0, len(attempts) - 1),
            "follow_ups": follow_ups,
            "follow_up_count": sum(len(f.get("texts") or []) for f in follow_ups),
            "error": error_text,
            "send": record.get("send") if isinstance(record.get("send"), dict) else {},
        }

    def _summary(self, entry: dict) -> dict:
        user_preview, _ = self._clip(entry.get("user_message", ""), self.PREVIEW_MAX)
        reply_preview, _ = self._clip(entry.get("reply", ""), self.PREVIEW_MAX)
        error_short, _ = self._clip(entry.get("error", ""), 120)
        send = entry.get("send") if isinstance(entry.get("send"), dict) else {}
        tokens = entry.get("tokens") if isinstance(entry.get("tokens"), dict) else {}
        return {
            "id": entry.get("id", ""),
            "time": entry.get("time", 0.0),
            "ok": bool(entry.get("ok")),
            "duration_ms": int(entry.get("duration_ms") or 0),
            "session_id": entry.get("session_id", ""),
            "context_type": entry.get("context_type", ""),
            "chat_id": entry.get("chat_id", ""),
            "model": entry.get("display_model") or entry.get("model") or "",
            "attempt_count": int(entry.get("attempt_count") or 0),
            "retry_count": int(entry.get("retry_count") or 0),
            "tool_call_count": int(entry.get("tool_call_count") or 0),
            "agent_rounds": int(entry.get("agent_rounds_total") or entry.get("agent_rounds") or 0),
            "tool_calls_truncated": bool(entry.get("tool_calls_truncated")),
            "follow_up_count": int(entry.get("follow_up_count") or 0),
            "total_tokens": int(tokens.get("total") or 0),
            "history_count": int(entry.get("history_count") or 0),
            "images": int(entry.get("images") or 0),
            "send_parts": int(send.get("parts") or 0),
            "user_preview": user_preview,
            "reply_preview": reply_preview,
            "error_short": error_short,
        }

    @staticmethod
    def _record_time(record: Any) -> float:
        """记录时间容错：损坏文件里的非法 time 不应让整个 store 永久报错。"""
        try:
            return float((record or {}).get("time") or 0.0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    # ==================== 持久化 ====================

    @staticmethod
    def _is_full_record(record: dict) -> bool:
        """判断 JSON 中的是旧版完整记录，而不是 v4 摘要。"""
        return any(key in record for key in (
            "system_prompt", "user_message", "reply", "history_overview",
            "tool_calls", "attempts", "follow_ups",
        ))

    @staticmethod
    def _ensure_detail_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS trace_details ("
            "id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
        )

    def _connect_detail_db(self) -> sqlite3.Connection:
        self.detail_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.detail_db_path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        self._ensure_detail_schema(conn)
        return conn

    def _write_details_db(self, details: dict[str, dict]) -> None:
        if not details:
            return
        rows = [
            (
                str(trace_id),
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                time.time(),
            )
            for trace_id, record in details.items()
            if trace_id
        ]
        if not rows:
            return
        with self._connect_detail_db() as conn:
            conn.executemany(
                "INSERT INTO trace_details(id, payload, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                rows,
            )

    def _delete_details_db(self, trace_ids: set[str]) -> None:
        ids = [(str(trace_id),) for trace_id in trace_ids if trace_id]
        if not ids or not self.detail_db_path.exists():
            return
        with self._connect_detail_db() as conn:
            conn.executemany("DELETE FROM trace_details WHERE id = ?", ids)

    def _read_detail_db(self, trace_id: str) -> Optional[dict]:
        if not trace_id or not self.detail_db_path.exists():
            return None
        try:
            with self._connect_detail_db() as conn:
                row = conn.execute(
                    "SELECT payload FROM trace_details WHERE id = ?", (trace_id,)
                ).fetchone()
            if not row:
                return None
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except Exception as e:
            print(f"读取 AI 追踪详情失败（{trace_id}）: {e}")
            return None

    def load(self) -> bool:
        """从 JSON 恢复摘要索引。文件不存在或损坏时静默从零开始。

        兼容旧版 v2 格式（带 texts 去重池 + *_ref 引用），也兼容旧版 v3 的内联全文。
        v3 全文会在下次保存时迁移进 SQLite。
        """
        try:
            if not self.path.exists() or self.path.stat().st_size <= 0:
                return False
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False
            enabled = bool(data.get("enabled", False))
            saved_max = data.get("max_records")
            raw_records = data.get("records")
            raw_records = [r for r in raw_records if isinstance(r, dict)] if isinstance(raw_records, list) else []
            # 旧格式的去重池：只在迁移时用一次
            legacy_texts = data.get("texts")
            legacy_texts = ({str(k): str(v) for k, v in legacy_texts.items()}
                            if isinstance(legacy_texts, dict) else {})
            if legacy_texts:
                raw_records = [self._migrate_v2_record(r, legacy_texts) for r in raw_records]
            summaries: list[dict] = []
            migrated_details: dict[str, dict] = {}
            for raw_record in raw_records:
                if self._is_full_record(raw_record):
                    full = self._sanitize_record(raw_record)
                    rid = str(full.get("id") or "")
                    summaries.append(self._summary(full))
                    if rid:
                        migrated_details[rid] = full
                else:
                    summaries.append(dict(raw_record))
            try:
                last_update = float(data.get("last_update") or time.time())
            except (TypeError, ValueError):
                last_update = time.time()
            with self._lock:
                self._enabled = enabled
                # 构造时未显式传上限才沿用文件值；显式传 100 也必须覆盖旧文件的 1000。
                if saved_max is not None and not self._max_records_explicit:
                    self._max_records = self._clamp_max_records(saved_max)
                self.records = deque(summaries, maxlen=self._max_records)
                kept_ids = {str(r.get("id") or "") for r in self.records if r.get("id")}
                self._pending_details = {
                    rid: detail for rid, detail in migrated_details.items() if rid in kept_ids
                }
                # 清理上次崩溃留下的孤儿详情，也清理因新上限缩小而被裁掉的旧详情。
                if self.detail_db_path.exists():
                    try:
                        with self._connect_detail_db() as conn:
                            db_ids = {str(row[0]) for row in conn.execute("SELECT id FROM trace_details")}
                        self._pending_deletes.update(db_ids - kept_ids)
                    except Exception as e:
                        print(f"检查 AI 追踪孤儿详情失败，将保留并稍后重试: {e}")
                self.last_update = last_update
                self._dirty = bool(self._pending_details or self._pending_deletes)
            return True
        except Exception as e:
            print(f"加载 AI 追踪记录失败，将从零开始: {e}")
            with self._lock:
                self._enabled = False
                self.records = deque(maxlen=self._max_records)
                self._pending_details.clear()
                self._pending_deletes.clear()
                self._dirty = False
            return False

    def _migrate_v2_record(self, entry: dict, texts: dict) -> dict:
        """把旧版 *_ref 引用展开成内联正文。"""
        out = dict(entry)
        for ref_key, text_key in (("prompt_ref", "system_prompt"),
                                  ("user_ref", "user_message"),
                                  ("reply_ref", "reply")):
            if ref_key in out:
                out[text_key] = texts.get(str(out.pop(ref_key) or ""), "")
        for item in out.get("history_overview") or []:
            if isinstance(item, dict) and "ref" in item:
                item["content"] = texts.get(str(item.pop("ref") or ""), "")
        for item in out.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            for ref_key, text_key in (("args_ref", "arguments"),
                                      ("result_ref", "result"),
                                      ("sent_ref", "sent_to_model")):
                if ref_key in item:
                    item[text_key] = texts.get(str(item.pop(ref_key) or ""), "")
        return out

    def save(self, force: bool = True) -> bool:
        """提交快照。磁盘 IO 不持有状态锁，新 trace 可并发进入内存队列。"""
        with self._save_lock:
            now = time.time()
            with self._lock:
                if not self._dirty and not force:
                    return True
                if not force and (now - self._last_save) < self.SAVE_INTERVAL_SECONDS:
                    return True
                snapshot_version = self._state_version
                pending_details = dict(self._pending_details)
                current_ids = {str(r.get("id") or "") for r in self.records}
                pending_deletes = set(self._pending_deletes) - current_ids
                payload = {
                    "version": 4,
                    "enabled": bool(self._enabled),
                    "max_records": self._max_records,
                    "records": copy.deepcopy(list(self.records)),
                    "last_update": float(self.last_update or now),
                }

            try:
                # 先保证索引将引用的详情已经存在。若随后写索引失败，重复 upsert 是安全的。
                self._write_details_db(pending_details)
                # 机器读的调试文件，不缩进：省约 10% 体积和序列化时间
                atomic_write_json(self.path, payload)
                # 索引成功落盘后再删淘汰详情；崩溃最多留下无害孤儿行。
                self._delete_details_db(pending_deletes)
            except Exception as e:
                print(f"保存 AI 追踪记录失败: {e}")
                return False

            with self._lock:
                # 只删除本次快照实际提交的版本；提交期间同 id 被更新时必须保留新值。
                for trace_id, detail in pending_details.items():
                    if self._pending_details.get(trace_id) is detail:
                        self._pending_details.pop(trace_id, None)
                self._pending_deletes.difference_update(pending_deletes)
                live_ids = {str(r.get("id") or "") for r in self.records}
                self._pending_deletes.difference_update(live_ids)
                self._dirty = bool(
                    self._state_version != snapshot_version
                    or self._pending_details
                    or self._pending_deletes
                )
                self._last_save = now
                # 提交期间若来了新修改，立即安排下一批，不必再等完整时间窗。
                if self._dirty:
                    self._flush_wakeup.set()
            return True

    def _flush_loop(self) -> None:
        """后台落盘线程：定时 flush，也支持积压过多时提前唤醒。"""
        while not self._stop_flush.is_set():
            self._flush_wakeup.wait(self.SAVE_INTERVAL_SECONDS)
            self._flush_wakeup.clear()
            if self._stop_flush.is_set():
                break
            try:
                self.save(force=True)
            except Exception as e:
                print(f"AI 追踪后台落盘线程异常，将继续重试: {e}")

    def start_flush_thread(self) -> None:
        with self._lock:
            if self._flush_thread is not None:
                return
            self._flush_thread = threading.Thread(
                target=self._flush_loop, name="XcBot-TraceFlush", daemon=True)
            self._flush_thread.start()

    def stop_flush_thread(self) -> None:
        self._stop_flush.set()
        self._flush_wakeup.set()


def create_trace_store(path: Optional[Path] = None, register_atexit: bool = True,
                       max_records: Optional[int] = None) -> TraceStore:
    """创建 TraceStore。register_atexit 时启动后台落盘线程并在退出时收尾。"""
    store = TraceStore(path=path, max_records=max_records)
    if register_atexit:
        store.start_flush_thread()

        def _shutdown():
            store.stop_flush_thread()
            store.save(force=True)

        atexit.register(_shutdown)
    return store
