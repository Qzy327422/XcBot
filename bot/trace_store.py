# -*- coding: utf-8 -*-
"""AI 对话追踪记录（JSON 持久化，滚动 24 小时窗口）。

只记录 AI 对话的调用链路，用于在 WebUI 追踪页排查：
消息发送链路、模型调用链路（含失败重试）、当次系统提示词、单条 token 统计。

体积控制思路：
- 24 小时滚动窗口 + 记录条数硬上限；
- 长文本（系统提示词、用户消息、回复、历史每条原文）按内容哈希存入去重池，
  记录里只留引用；相同内容多轮复用同一份，避免 O(n²) 膨胀；
- 单条文本与整个池都有长度上限，超出即截断。

隐私说明：这里**会**持久化对话原文——系统提示词、用户消息、模型回复和上下文
历史都能在追踪页完整查看。默认关闭；开启后 data/ 目录下的追踪文件包含聊天内容，
分享日志或备份前请注意。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import threading
import time
import uuid
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
    """AI 对话追踪存储。线程安全，节流落盘。"""

    SAVE_PATH = Path(str(BASE_DIR)) / "data" / "ai_trace.json"
    # 节流：最多每 N 秒写一次盘，与 token_stats 同量级
    SAVE_INTERVAL_SECONDS = 5.0
    # 滚动窗口：过去 24 小时
    WINDOW_SECONDS = 24 * 3600
    # 24 小时内的记录条数硬上限，防止高频群把文件顶到几十 MB
    MAX_RECORDS = 300
    # 去重池字符数上限。条数上限管不住体积，这个才是真正的字节预算：
    # 6M 字符 ≈ 6MB 文件，超出时从最旧记录开始丢。
    MAX_POOL_CHARS = 6_000_000
    # 单条记录最多保留的重试链路条数
    MAX_ATTEMPTS = 20

    SYSTEM_PROMPT_MAX = 6000
    # 用户消息、模型回复、历史正文共用同一截断上限。
    # 必须一致：第 N 轮的消息/回复就是第 N+1 轮的历史条目，
    # 只有截断后完全相同才会命中同一个哈希，去重才有效。
    TEXT_MAX = 4000
    ERROR_MAX = 400
    PREVIEW_MAX = 80
    # 历史保留条数，与 context_max_messages 默认值同量级。
    # 正文走去重池，重复内容不额外占空间，因此不必压得很低。
    HISTORY_ITEMS_MAX = 60
    # Agent 工具调用明细：条数上限 + 单条参数/结果的截断长度。
    # 30 轮循环每轮可能并发多个工具，不限条数会让单条记录膨胀。
    TOOL_CALLS_MAX = 80
    TOOL_ARGS_MAX = 1000
    TOOL_RESULT_MAX = 2000

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else self.SAVE_PATH
        # 默认关闭：避免升级后静默开始落盘用户对话内容
        self._enabled = False
        self.records: list[dict] = []
        # 正文去重池：hash -> 全文。系统提示词、用户消息、模型回复、
        # 历史条目共用，同一段话在多条记录里只存一份。
        self.texts: dict[str, str] = {}
        self.last_update = time.time()
        self._dirty = False
        self._last_save = 0.0
        self._lock = threading.RLock()
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_flush = threading.Event()
        self.load()

    # ==================== 开关 ====================

    @property
    def enabled(self) -> bool:
        """主流程热路径只读这个属性，零文件 IO。"""
        return bool(self._enabled)

    def set_enabled(self, value: bool) -> bool:
        with self._lock:
            self._enabled = bool(value)
            self.last_update = time.time()
            self._dirty = True
            self._maybe_save_locked(force=True)
            return self._enabled

    # ==================== 写入 ====================

    def add_record(self, record: dict) -> str:
        """写入一条追踪记录，返回 trace_id。

        只做内存操作并标记脏位，落盘交给后台 flush 线程，
        避免在 asyncio 事件循环线程上做同步 fsync 拖慢所有会话的回复。
        """
        if not isinstance(record, dict):
            return ""
        with self._lock:
            entry = self._sanitize_record_locked(record)
            self.records.append(entry)
            self.last_update = time.time()
            self._dirty = True
            self._prune_window_locked()
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

    def list_records(self, limit: int = 200) -> dict:
        """返回摘要列表，全文只在 get_record 提供。"""
        with self._lock:
            self._prune_window_locked()
            try:
                count = max(1, min(int(limit or 200), 500))
            except (TypeError, ValueError):
                count = 200
            rows = [self._summary_locked(entry) for entry in self.records[-count:]]
            rows.reverse()  # 最新在前
            # 高频场景下条数上限先到，实际覆盖时长会远小于 24 小时，
            # 这里回报真实跨度，避免界面上写着 24 小时却只有两小时数据。
            span_hours = 0.0
            if self.records:
                oldest = self._record_time(self.records[0])
                if oldest > 0:
                    span_hours = round(max(0.0, time.time() - oldest) / 3600.0, 1)
            return {
                "enabled": self.enabled,
                "window_hours": int(self.WINDOW_SECONDS // 3600),
                "span_hours": span_hours,
                "count": len(self.records),
                "max_records": int(self.MAX_RECORDS),
                "pool_chars": self._pool_chars(),
                "records": rows,
            }

    def get_record(self, trace_id: str) -> Optional[dict]:
        """返回完整记录，正文已从去重池还原。"""
        key = str(trace_id or "")
        if not key:
            return None
        with self._lock:
            self._prune_window_locked()
            for entry in reversed(self.records):
                if entry.get("id") != key:
                    continue
                out = json.loads(json.dumps(entry, ensure_ascii=False))
                out["system_prompt"] = self._text_of(out.pop("prompt_ref", ""))
                out["user_message"] = self._text_of(out.pop("user_ref", ""))
                out["reply"] = self._text_of(out.pop("reply_ref", ""))
                for item in out.get("history_overview") or []:
                    if isinstance(item, dict):
                        item["content"] = self._text_of(item.pop("ref", ""))
                for item in out.get("tool_calls") or []:
                    if isinstance(item, dict):
                        item["arguments"] = self._text_of(item.pop("args_ref", ""))
                        item["result"] = self._text_of(item.pop("result_ref", ""))
                        item["sent_to_model"] = self._text_of(item.pop("sent_ref", ""))
                return out
            return None

    def stats(self) -> dict:
        with self._lock:
            self._prune_window_locked()
            ok = sum(1 for r in self.records if r.get("ok"))
            return {
                "enabled": self.enabled,
                "count": len(self.records),
                "ok": ok,
                "failed": len(self.records) - ok,
                "texts": len(self.texts),
            }

    def clear(self) -> None:
        with self._lock:
            self.records = []
            self.texts = {}
            self.last_update = time.time()
            self._dirty = True
            self._maybe_save_locked(force=True)

    # ==================== 内部 ====================

    def _window_start(self, now: float | None = None) -> float:
        return float(now if now is not None else time.time()) - float(self.WINDOW_SECONDS)

    @staticmethod
    def _clip(text: Any, limit: int) -> tuple[str, bool]:
        raw = str(text or "")
        if len(raw) <= limit:
            return raw, False
        return raw[:limit], True

    def _put_text_locked(self, text: str, limit: int) -> tuple[str, int, bool]:
        """正文入去重池，返回 (引用键, 原始字数, 是否截断)。

        系统提示词、用户消息、模型回复、历史条目共用一个池：
        同一段话在"本轮消息"和"下一轮历史"里只会存一份。
        """
        raw = str(text or "")
        chars = len(raw)
        clipped, truncated = self._clip(raw, limit)
        if not clipped:
            return "", chars, truncated
        ref = hashlib.sha1(clipped.encode("utf-8", errors="replace")).hexdigest()[:16]
        if ref not in self.texts:
            self.texts[ref] = clipped
        return ref, chars, truncated

    def _text_of(self, ref: Any) -> str:
        return self.texts.get(str(ref or ""), "")

    def _sanitize_attempts_locked(self, attempts: Any) -> list[dict]:
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

    def _sanitize_tool_calls_locked(self, calls: Any) -> list[dict]:
        """清洗 Agent 工具调用明细。

        参数和结果都走去重池：同一个工具被反复调用时（比如模型卡在重复调用上）
        相同的参数只会存一份。
        """
        rows: list[dict] = []
        if not isinstance(calls, list):
            return rows
        for item in calls[-self.TOOL_CALLS_MAX:]:
            if not isinstance(item, dict):
                continue
            sent_ref, sent_chars, sent_cut = self._put_text_locked(
                item.get("sent_to_model", ""), self.TOOL_RESULT_MAX)
            args_ref, args_chars, args_cut = self._put_text_locked(
                item.get("arguments", ""), self.TOOL_ARGS_MAX)
            result_ref, result_chars, result_cut = self._put_text_locked(
                item.get("result", ""), self.TOOL_RESULT_MAX)
            rows.append({
                "round": int(item.get("round") or 0),
                "index": int(item.get("index") or 0),
                "name": str(item.get("name") or ""),
                "args_ref": args_ref,
                "args_chars": args_chars,
                "args_truncated": args_cut,
                "result_ref": result_ref,
                "result_chars": result_chars,
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
                "sent_ref": sent_ref,
                "sent_chars": sent_chars,
                "sent_truncated": sent_cut,
            })
        return rows

    def _sanitize_record_locked(self, record: dict) -> dict:
        now = time.time()
        prompt_ref, prompt_chars, _ = self._put_text_locked(
            record.get("system_prompt", ""), self.SYSTEM_PROMPT_MAX)
        user_ref, user_chars, user_truncated = self._put_text_locked(
            record.get("user_message", ""), self.TEXT_MAX)
        reply_ref, reply_chars, reply_truncated = self._put_text_locked(
            record.get("reply", ""), self.TEXT_MAX)
        error_text, _ = self._clip(record.get("error", ""), self.ERROR_MAX)

        overview: list[dict] = []
        raw_overview = record.get("history_overview")
        if isinstance(raw_overview, list):
            for item in raw_overview[-self.HISTORY_ITEMS_MAX:]:
                if not isinstance(item, dict):
                    continue
                ref, chars, truncated = self._put_text_locked(item.get("content", ""), self.TEXT_MAX)
                overview.append({
                    "role": str(item.get("role") or "user"),
                    "chars": int(item.get("chars") or chars),
                    "ref": ref,
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

        attempts = self._sanitize_attempts_locked(record.get("attempts"))
        raw_tool_calls = record.get("tool_calls") if isinstance(record.get("tool_calls"), list) else []
        tool_calls = self._sanitize_tool_calls_locked(raw_tool_calls)
        chat_id = record.get("chat_id")

        return {
            "id": str(record.get("id") or uuid.uuid4().hex[:12]),
            "time": float(record.get("time") or now),
            "ok": bool(record.get("ok")),
            "duration_ms": int(record.get("duration_ms") or 0),
            "session_id": str(record.get("session_id") or ""),
            "context_type": str(record.get("context_type") or ""),
            "chat_id": str(chat_id) if chat_id is not None else "",
            "prompt_ref": prompt_ref,
            "prompt_chars": prompt_chars,
            "user_ref": user_ref,
            "user_chars": user_chars,
            "user_truncated": user_truncated,
            "reply_ref": reply_ref,
            "reply_chars": reply_chars,
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
            "error": error_text,
            "send": record.get("send") if isinstance(record.get("send"), dict) else {},
        }

    def _summary_locked(self, entry: dict) -> dict:
        user_preview, _ = self._clip(self._text_of(entry.get("user_ref")), self.PREVIEW_MAX)
        reply_preview, _ = self._clip(self._text_of(entry.get("reply_ref")), self.PREVIEW_MAX)
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
            "total_tokens": int(tokens.get("total") or 0),
            "history_count": int(entry.get("history_count") or 0),
            "images": int(entry.get("images") or 0),
            "send_parts": int(send.get("parts") or 0),
            "user_preview": user_preview,
            "reply_preview": reply_preview,
            "error_short": error_short,
        }

    def _prune_window_locked(self) -> bool:
        """裁掉窗口外记录、超出条数/字节上限的旧记录，并回收无引用正文。"""
        start = self._window_start()
        kept = [r for r in self.records if self._record_time(r) >= start]
        if len(kept) > self.MAX_RECORDS:
            kept = kept[-self.MAX_RECORDS:]
        changed = len(kept) != len(self.records)
        if changed:
            self.records = kept
        # 字节预算：条数上限管不住体积（单条理论上可达 254K 字符），
        # 这里按去重池实际字符数从最旧的记录开始丢，保证文件不会失控。
        if self._pool_chars() > self.MAX_POOL_CHARS:
            while len(self.records) > 1 and self._pool_chars() > self.MAX_POOL_CHARS:
                self.records.pop(0)
                self._recycle_texts_locked()
                changed = True
        if changed:
            self._recycle_texts_locked()
            self._dirty = True
        return changed

    @staticmethod
    def _record_time(record: Any) -> float:
        """记录时间容错：损坏文件里的非法 time 不应让整个 store 永久报错。"""
        try:
            return float((record or {}).get("time") or 0.0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _pool_chars(self) -> int:
        return sum(len(v) for v in self.texts.values())

    def _recycle_texts_locked(self) -> None:
        alive: set[str] = set()
        for r in self.records:
            for key in ("prompt_ref", "user_ref", "reply_ref"):
                ref = str(r.get(key) or "")
                if ref:
                    alive.add(ref)
            for item in r.get("history_overview") or []:
                if isinstance(item, dict):
                    ref = str(item.get("ref") or "")
                    if ref:
                        alive.add(ref)
            # 工具明细的参数与结果也在同一个去重池里，漏掉这里会把它们
            # 当成孤儿回收掉，详情页就只剩空白
            for item in r.get("tool_calls") or []:
                if isinstance(item, dict):
                    for key in ("args_ref", "result_ref", "sent_ref"):
                        ref = str(item.get(key) or "")
                        if ref:
                            alive.add(ref)
        # 不能用 len(alive) < len(self.texts) 做短路：空 ref 会把 alive 垫高一位，
        # 刚好抵掉一个孤儿，导致回收被跳过、MAX_POOL_CHARS 上限失效，
        # 进而让 _prune_window_locked 的 while 一路 pop 到只剩一条记录。
        # 空 ref 已在上面过滤掉，这里直接按集合重建。
        if len(self.texts) != len(alive):
            self.texts = {k: v for k, v in self.texts.items() if k in alive}

    # ==================== 持久化 ====================

    def load(self) -> bool:
        """从 JSON 恢复。文件不存在或损坏时静默从零开始。"""
        try:
            if not self.path.exists() or self.path.stat().st_size <= 0:
                return False
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False
            enabled = bool(data.get("enabled", False))
            texts = data.get("texts")
            texts = {str(k): str(v) for k, v in texts.items()} if isinstance(texts, dict) else {}
            records = data.get("records")
            records = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
            try:
                last_update = float(data.get("last_update") or time.time())
            except (TypeError, ValueError):
                last_update = time.time()
            with self._lock:
                self._enabled = enabled
                self.texts = texts
                self.records = records
                self.last_update = last_update
                self._prune_window_locked()
                # 旧版本或手工编辑的文件可能带孤儿正文，加载后统一回收一次
                self._recycle_texts_locked()
            return True
        except Exception as e:
            print(f"加载 AI 追踪记录失败，将从零开始: {e}")
            with self._lock:
                self._enabled = False
                self.records = []
                self.texts = {}
            return False

    def save(self, force: bool = True) -> bool:
        with self._lock:
            self._prune_window_locked()
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
                "version": 2,
                "enabled": bool(self._enabled),
                "window_seconds": int(self.WINDOW_SECONDS),
                "texts": dict(self.texts),
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


def create_trace_store(path: Optional[Path] = None, register_atexit: bool = True) -> TraceStore:
    """创建 TraceStore。register_atexit 时启动后台落盘线程并在退出时收尾。"""
    store = TraceStore(path=path)
    if register_atexit:
        store.start_flush_thread()

        def _shutdown():
            store.stop_flush_thread()
            store.save(force=True)

        atexit.register(_shutdown)
    return store
