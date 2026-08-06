# -*- coding: utf-8 -*-
"""AI 对话追踪记录（JSON 持久化，固定条数环形缓冲）。

只记录 AI 对话的调用链路，用于在 WebUI 追踪页排查：
消息发送链路、模型调用链路（含失败重试）、当次系统提示词、
Agent 工具调用历史（工具名/参数/结果）、单条 token 统计。

体积控制思路（照 AstrBot 的轻量化设计）：
- 只有一个维度：记录条数上限（默认 100 条，WebUI 可调）。超出即丢最旧的。
  不再有 24 小时窗口，也不再有去重池字节预算——那两个维度需要频繁全量扫描
  才能维护，是低配机上的 CPU 大头，而条数上限本身已经足够控制体积。
- 正文直接内联存在记录里，不走内容哈希去重池。去重省下的空间远不值得每次
  写入都做哈希 + 每次淘汰都做全量引用扫描（O(记录数 × 每条引用数)）。
- 每类文本各有截断上限，超出即截断，单条记录的体积因此是有界的。

隐私说明：这里**会**持久化对话原文——系统提示词、用户消息、模型回复、上下文
历史和工具调用参数/结果都能在追踪页完整查看。默认关闭；开启后 data/ 目录下的
追踪文件包含聊天内容，分享日志或备份前请注意。
"""
from __future__ import annotations

import atexit
import copy
import json
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

    SYSTEM_PROMPT_MAX = 6000
    TEXT_MAX = 4000
    ERROR_MAX = 400
    PREVIEW_MAX = 80
    # 历史条目：条数与单条长度都比当前消息收得更紧。
    # 取消去重池后正文内联存储，历史是最容易膨胀的部分（每条记录都带一份完整
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
        self._max_records = self._clamp_max_records(max_records)
        # deque(maxlen) 自动挤掉最旧的，不需要任何裁剪逻辑
        self.records: deque = deque(maxlen=self._max_records)
        self.last_update = time.time()
        self._dirty = False
        self._last_save = 0.0
        self._lock = threading.RLock()
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_flush = threading.Event()
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
            self._dirty = True
            self._maybe_save_locked(force=True)
            return self._enabled

    def set_max_records(self, value: Any) -> int:
        """调整条数上限。deque 换新的，超出部分自动丢最旧的。"""
        new_max = self._clamp_max_records(value)
        with self._lock:
            if new_max == self._max_records:
                return self._max_records
            self._max_records = new_max
            self.records = deque(self.records, maxlen=new_max)
            self.last_update = time.time()
            self._dirty = True
            self._maybe_save_locked(force=True)
            return self._max_records

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
            # deque(maxlen) 满了会自动弹出最左侧（最旧）的，无需裁剪
            self.records.append(entry)
            self.last_update = time.time()
            self._dirty = True
            return str(entry.get("id") or "")

    def attach_send(self, trace_id: str, parts: int, message_ids: Optional[list] = None) -> bool:
        """回填分段发送结果。记录已被窗口裁掉时返回 False。"""
        key = str(trace_id or "")
        if not key:
            return False
        with self._lock:
            for entry in reversed(self.records):
                if entry.get("id") != key:
                    continue
                ids = []
                for mid in (message_ids or [])[:20]:
                    if mid is not None:
                        ids.append(mid)
                entry["send"] = {
                    "parts": max(0, int(parts or 0)),
                    "message_ids": ids,
                    "time": time.time(),
                }
                self._dirty = True
                return True
            return False

    # ==================== 读取（供 WebUI） ====================

    def list_records(self, limit: Optional[int] = None) -> dict:
        """返回摘要列表，全文只在 get_record 提供。"""
        with self._lock:
            try:
                count = int(limit) if limit else self._max_records
            except (TypeError, ValueError):
                count = self._max_records
            count = max(1, min(count, self._max_records))
            # deque 不支持负索引切片，转 list 再切
            rows = [self._summary(e) for e in list(self.records)[-count:]]
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
        """返回完整记录。正文内联存储，直接深拷贝返回。"""
        key = str(trace_id or "")
        if not key:
            return None
        with self._lock:
            for entry in reversed(self.records):
                if entry.get("id") == key:
                    return copy.deepcopy(entry)
            return None

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
            self.records.clear()
            self.last_update = time.time()
            self._dirty = True
            self._maybe_save_locked(force=True)

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

    def load(self) -> bool:
        """从 JSON 恢复。文件不存在或损坏时静默从零开始。

        兼容旧版 v2 格式（带 texts 去重池 + *_ref 引用）：加载时把引用展开成内联
        正文，之后就再也不需要那套机制了。
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
            try:
                last_update = float(data.get("last_update") or time.time())
            except (TypeError, ValueError):
                last_update = time.time()
            with self._lock:
                self._enabled = enabled
                # 文件里存过上限就沿用；构造时显式传了非默认值则以构造参数为准
                if saved_max is not None and self._max_records == self.DEFAULT_MAX_RECORDS:
                    self._max_records = self._clamp_max_records(saved_max)
                self.records = deque(raw_records, maxlen=self._max_records)
                self.last_update = last_update
            return True
        except Exception as e:
            print(f"加载 AI 追踪记录失败，将从零开始: {e}")
            with self._lock:
                self._enabled = False
                self.records = deque(maxlen=self._max_records)
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
        with self._lock:
            return self._maybe_save_locked(force=force)

    def _maybe_save_locked(self, force: bool = False) -> bool:
        """调用方需持有 self._lock。"""
        if not self._dirty and not force:
            return True
        now = time.time()
        if not force and (now - self._last_save) < self.SAVE_INTERVAL_SECONDS:
            return True
        try:
            payload = {
                "version": 3,
                "enabled": bool(self._enabled),
                "max_records": self._max_records,
                "records": list(self.records),
                "last_update": float(self.last_update or now),
            }
            # 机器读的调试文件，不缩进：省约 10% 体积和序列化时间
            atomic_write_json(self.path, payload)
            self._dirty = False
            self._last_save = now
            return True
        except Exception as e:
            print(f"保存 AI 追踪记录失败: {e}")
            return False

    def _flush_loop(self) -> None:
        """后台落盘线程：把 fsync 从事件循环线程挪走。"""
        while not self._stop_flush.wait(self.SAVE_INTERVAL_SECONDS):
            try:
                with self._lock:
                    if not self._dirty:
                        continue
                    self._maybe_save_locked(force=True)
            except Exception:
                pass

    def start_flush_thread(self) -> None:
        with self._lock:
            if self._flush_thread is not None:
                return
            self._flush_thread = threading.Thread(
                target=self._flush_loop, name="XcBot-TraceFlush", daemon=True)
            self._flush_thread.start()

    def stop_flush_thread(self) -> None:
        self._stop_flush.set()


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
