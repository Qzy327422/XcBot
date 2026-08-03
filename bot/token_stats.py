# -*- coding: utf-8 -*-
"""Global token usage statistics with JSON persistence.

Counters are a rolling past-24-hours window (not calendar-day reset).
"""
from __future__ import annotations

import atexit
import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from bot.io_json import atomic_write_json
from bot.paths import BASE_DIR


class TokenStats:
    """真实的 Token 统计管理器（JSON 持久化，滚动 24 小时窗口）。"""

    SAVE_PATH = Path(str(BASE_DIR)) / "data" / "token_stats.json"
    # 节流：最多每 N 秒写一次盘，避免高频 API 打满磁盘
    SAVE_INTERVAL_SECONDS = 5.0
    # 不设每会话条数上限；24h 窗口本身负责控制生命周期。
    # 旧版 500 条上限会让高频会话的 24h Token 严重少算。
    DETAIL_LIMIT = 0
    # 滚动窗口：过去 24 小时
    WINDOW_SECONDS = 24 * 3600
    # 窗口裁剪的最小间隔。裁剪要全量扫明细，太频繁会让记账本身成为 CPU 大头；
    # 读统计和落盘前会强制裁一次，所以节流不影响对外数字的准确性。
    PRUNE_INTERVAL_SECONDS = 60.0

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else self.SAVE_PATH
        self.total_tokens = 0
        self.session_tokens = defaultdict(int)
        self.user_tokens = defaultdict(int)
        self.group_tokens = defaultdict(int)
        self.detailed_stats = defaultdict(list)
        self.last_update = time.time()
        self._dirty = False
        self._last_save = 0.0
        self._last_prune = 0.0
        self._lock = threading.RLock()
        self.load()

    def _window_start(self, now: float | None = None) -> float:
        return float(now if now is not None else time.time()) - float(self.WINDOW_SECONDS)

    def _rebuild_aggregates_locked(self) -> None:
        """根据 detailed_stats 重建聚合计数（调用方需持有 lock）。"""
        total = 0
        session_tokens: dict[str, int] = defaultdict(int)
        user_tokens: dict[str, int] = defaultdict(int)
        group_tokens: dict[str, int] = defaultdict(int)
        for sid, rows in self.detailed_stats.items():
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tokens = int(row.get("tokens") or 0)
                if tokens <= 0:
                    continue
                total += tokens
                session_tokens[str(sid)] += tokens
                uid = row.get("user_id")
                gid = row.get("group_id")
                if uid is not None and str(uid) not in {"", "None"}:
                    user_tokens[str(uid)] += tokens
                if gid is not None and str(gid) not in {"", "None"}:
                    group_tokens[str(gid)] += tokens
        self.total_tokens = total
        self.session_tokens = defaultdict(int, session_tokens)
        self.user_tokens = defaultdict(int, user_tokens)
        self.group_tokens = defaultdict(int, group_tokens)

    def _add_to_aggregates_locked(self, sid: str, tokens: int, prompt_tokens: int,
                                  completion_tokens: int, model: str,
                                  user_id=None, group_id=None) -> None:
        """把一条新记录累加进聚合值（调用方需持有 lock）。

        原来每次记账都调 _rebuild_aggregates_locked 全量重扫明细，复杂度 O(n²)：
        实测 250/500/1000/2000 次记录分别耗时 0.03/0.13/0.52/2.19 秒，
        条数翻倍耗时翻四倍。窗口裁剪真的裁掉数据时仍会全量重建，
        所以聚合值不会随时间漂移。
        """
        if tokens <= 0:
            return
        self.total_tokens += tokens
        self.session_tokens[str(sid)] += tokens
        if user_id is not None and str(user_id) not in {"", "None"}:
            self.user_tokens[str(user_id)] += tokens
        if group_id is not None and str(group_id) not in {"", "None"}:
            self.group_tokens[str(group_id)] += tokens

    def _prune_window_locked(self, force: bool = False) -> bool:
        """裁掉窗口外明细并重建聚合。返回是否有变更。

        force=False 时按 PRUNE_INTERVAL_SECONDS 节流：这个函数要逐会话逐条扫描
        整个 24 小时窗口，而 add_usage 每写一条都调它一次，累计 n 条就是
        1+2+…+n 次检查——实测 4000 次记账要将近 1 秒，条数翻倍耗时翻四倍。
        读统计（get_stats）和落盘前仍会 force=True 强制裁一次，
        所以窗口边界不会因为节流而失准。
        """
        now = time.time()
        if not force and (now - self._last_prune) < self.PRUNE_INTERVAL_SECONDS:
            return False
        self._last_prune = now
        cutoff = self._window_start(now)
        changed = False
        empty_sids = []
        for sid, rows in list(self.detailed_stats.items()):
            if not isinstance(rows, list):
                self.detailed_stats[sid] = []
                changed = True
                continue
            kept = []
            for row in rows:
                if not isinstance(row, dict):
                    changed = True
                    continue
                try:
                    ts = float(row.get("time") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                    changed = True
                if ts >= cutoff:
                    kept.append(row)
                else:
                    changed = True
            if self.DETAIL_LIMIT > 0 and len(kept) > self.DETAIL_LIMIT:
                kept = kept[-self.DETAIL_LIMIT:]
                changed = True
            if len(kept) != len(rows):
                changed = True
            self.detailed_stats[sid] = kept
            if not kept:
                empty_sids.append(sid)
        for sid in empty_sids:
            self.detailed_stats.pop(sid, None)
            changed = True
        # 只在真的裁掉了东西时才全量重建；没变化就没必要重扫
        if changed:
            self._rebuild_aggregates_locked()
            self._dirty = True
        return changed

    def add_usage(
        self,
        session_id: str,
        user_id: int = None,
        group_id: int = None,
        tokens: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str = "",
    ):
        """记录真实的 Token 使用情况（进入滚动 24h 窗口）。"""
        if tokens <= 0:
            return
        with self._lock:
            # 写入前先尽量裁旧数据；throttle 命中时也不致命，因为后面会 rebuild
            self._prune_window_locked(force=False)
            now = time.time()
            sid = str(session_id or "").strip() or "unknown"
            self.detailed_stats[sid].append({
                "time": now,
                "tokens": int(tokens),
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "model": model,
                "user_id": user_id,
                "group_id": group_id,
            })
            if self.DETAIL_LIMIT > 0 and len(self.detailed_stats[sid]) > self.DETAIL_LIMIT:
                self.detailed_stats[sid] = self.detailed_stats[sid][-self.DETAIL_LIMIT:]
            self.last_update = now
            self._dirty = True
            self._add_to_aggregates_locked(
                sid, int(tokens), int(prompt_tokens or 0), int(completion_tokens or 0),
                model, user_id, group_id,
            )
            self._maybe_save_locked(force=False)

    def get_stats(self, session_id: str = None, user_id: int = None, group_id: int = None) -> dict:
        """获取过去 24 小时 Token 统计。"""
        with self._lock:
            # 读取路径强制裁剪，保证展示值始终是真 24h 窗口
            self._prune_window_locked(force=True)
            if session_id:
                sid = str(session_id)
                rows = self.detailed_stats.get(sid, [])
                return {
                    "session_tokens": int(self.session_tokens.get(sid, 0) or 0),
                    "session_calls": len(rows),
                    "last_call": rows[-1].get("time", 0) if rows else 0,
                    "window_hours": 24,
                }
            if user_id is not None:
                return {
                    "user_tokens": int(self.user_tokens.get(str(user_id), 0) or 0),
                    "window_hours": 24,
                }
            if group_id is not None:
                return {
                    "group_tokens": int(self.group_tokens.get(str(group_id), 0) or 0),
                    "window_hours": 24,
                }
            return {
                "total_tokens": int(self.total_tokens or 0),
                "sessions": len(self.session_tokens),
                "users": len(self.user_tokens),
                "groups": len(self.group_tokens),
                "total_calls": sum(len(calls) for calls in self.detailed_stats.values()),
                "window_hours": 24,
            }

    def reset(self):
        """清空窗口统计并立即落盘。"""
        with self._lock:
            self.total_tokens = 0
            self.session_tokens.clear()
            self.user_tokens.clear()
            self.group_tokens.clear()
            self.detailed_stats.clear()
            self.last_update = time.time()
            self._dirty = True
            self._maybe_save_locked(force=True)

    def load(self) -> bool:
        """从 JSON 恢复。文件不存在或损坏时静默从零开始。"""
        try:
            if not self.path.exists() or self.path.stat().st_size <= 0:
                return False
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False
            with self._lock:
                detailed = data.get("detailed_stats") or {}
                self.detailed_stats = defaultdict(list)
                if isinstance(detailed, dict):
                    for sid, rows in detailed.items():
                        if not isinstance(rows, list):
                            continue
                        cleaned = [row for row in rows if isinstance(row, dict)]
                        if self.DETAIL_LIMIT > 0:
                            cleaned = cleaned[-self.DETAIL_LIMIT:]
                        self.detailed_stats[str(sid)] = cleaned
                self.last_update = float(data.get("last_update") or time.time())
                # 兼容旧文件：若没有明细，则从空窗口开始
                if self.detailed_stats:
                    self._prune_window_locked(force=True)
                    # prune 若未裁掉任何行，changed=False 不会重建聚合；加载后必须重建一次
                    self._rebuild_aggregates_locked()
                else:
                    self.total_tokens = 0
                    self.session_tokens = defaultdict(int)
                    self.user_tokens = defaultdict(int)
                    self.group_tokens = defaultdict(int)
                self._dirty = True  # 迁移后写回新格式
                self._maybe_save_locked(force=True)
            return True
        except Exception as e:
            print(f"加载 Token 统计失败，将从零开始: {e}")
            return False

    def save(self, force: bool = True) -> bool:
        """落盘当前统计。"""
        with self._lock:
            # 保存前强制裁剪，避免把窗口外明细写回磁盘
            self._prune_window_locked(force=True)
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
                "window_seconds": int(self.WINDOW_SECONDS),
                "total_tokens": int(self.total_tokens or 0),
                "session_tokens": {str(k): int(v or 0) for k, v in self.session_tokens.items()},
                "user_tokens": {str(k): int(v or 0) for k, v in self.user_tokens.items()},
                "group_tokens": {str(k): int(v or 0) for k, v in self.group_tokens.items()},
                "detailed_stats": {
                    str(sid): (list(rows[-self.DETAIL_LIMIT:]) if self.DETAIL_LIMIT > 0 else list(rows))
                    for sid, rows in self.detailed_stats.items()
                    if rows
                },
                "last_update": float(self.last_update or now),
            }
            atomic_write_json(self.path, payload, indent=2)
            self._dirty = False
            self._last_save = now
            return True
        except Exception as e:
            print(f"保存 Token 统计失败: {e}")
            return False


def create_token_stats(path: Optional[Path] = None, register_atexit: bool = True) -> TokenStats:
    """Create TokenStats and optionally register atexit save."""
    stats = TokenStats(path=path)
    if register_atexit:
        atexit.register(lambda: stats.save(force=True))
    return stats
